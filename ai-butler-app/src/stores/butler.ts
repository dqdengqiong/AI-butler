import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  butlerApi,
  type ApiObject,
  type MessageResponse,
  type SendMessageResponse,
} from '@/api/butler'
import { ApiError } from '@/api/client'
import { connectRunStream, type RunStreamEvent } from '@/stream/transport'
import {
  asArray,
  asObject,
  mapAgentDefinition,
  mapConversation,
  mapMessage,
  mapPlan,
  mapTask,
  numberValue,
  stringValue,
} from '@/stores/butler-projections'
import type {
  AgentShortcutCode,
  AgentShortcutViewModel,
  ChatItem,
  ConversationViewModel,
  PlanViewModel,
  TaskViewModel,
} from '@/types/view-models'

const progressLabels: Record<string, string> = {
  SEARCHING_WEB: '正在检索网络',
  RETRIEVING_PRIVATE: '正在检索我的资料',
  ORGANIZING_CITATIONS: '正在整理引用',
  GENERATING_ANSWER: '正在生成回答',
}

export interface SendMessageOptions {
  clientMessageId?: string
  contextPolicy?: 'AUTO' | 'CONTINUE_CURRENT' | 'ARCHIVE_AND_START'
  executionPolicy?: 'REJECT' | 'CANCEL_OTHER'
}

export type AssistantSceneTarget =
  { kind: 'GENERAL' } | { kind: 'SPECIALIST'; specialistCode: AgentShortcutCode }

export type AssistantSceneTransition = 'CURRENT' | 'RESUMABLE' | 'WELCOME' | 'CONFIRMATION_REQUIRED'

type StagedAssistantScene = AssistantSceneTarget | null

const executingRunStatuses = ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED']
const suspendedRunStatuses = ['AWAITING_INPUT', 'AWAITING_APPROVAL', 'FAILED_RETRYABLE']

function conversationMatchesScene(
  conversation: ConversationViewModel,
  target: AssistantSceneTarget,
): boolean {
  return target.kind === 'GENERAL'
    ? conversation.agentCode === undefined
    : conversation.agentCode === target.specialistCode
}

function stagedSceneMatches(staged: StagedAssistantScene, target: AssistantSceneTarget): boolean {
  if (!staged) return false
  if (staged.kind === 'GENERAL' || target.kind === 'GENERAL') {
    return staged.kind === target.kind
  }
  return staged.specialistCode === target.specialistCode
}

/**
 * 服务端事实 Store。SSE 文本只用于临时展示；收到 message.completed 后总是重新
 * 拉取消息，以服务端完整内容覆盖增量，避免重连或 attempt reset 造成拼接错误。
 */
export const useButlerStore = defineStore('butler', () => {
  const plans = ref<PlanViewModel[]>([])
  const tasks = ref<TaskViewModel[]>([])
  const chatItems = ref<ChatItem[]>([])
  const agentShortcuts = ref<AgentShortcutViewModel[]>([])
  const conversations = ref<ConversationViewModel[]>([])
  const activeConversationId = ref<string | null>(null)
  const stagedScene = ref<StagedAssistantScene>(null)
  const stagedSpecialistCode = computed(() =>
    stagedScene.value?.kind === 'SPECIALIST' ? stagedScene.value.specialistCode : null,
  )
  const loading = ref(false)
  const error = ref<string | null>(null)
  const activeRunId = ref<string | null>(null)
  const streamConversationId = ref<string | null>(null)
  const streamConnection = ref<{ close(): void } | null>(null)

  const pendingTasks = computed(() => tasks.value.filter((task) => !task.done))

  async function load(accessToken: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const [dashboard, definitions, conversationList] = await Promise.all([
        butlerApi.dashboard(accessToken),
        butlerApi.agentDefinitions(accessToken),
        butlerApi.conversations(accessToken),
      ])
      plans.value = asArray(dashboard.plans).map(mapPlan)
      tasks.value = asArray(dashboard.today_tasks).map(mapTask)
      agentShortcuts.value = definitions.items.map(mapAgentDefinition)
      conversations.value = conversationList.items.map(mapConversation)
      const current = conversationList.items.find((item) => item.status === 'CURRENT')
      activeConversationId.value = current?.id ?? conversationList.items[0]?.id ?? null
      stagedScene.value = null
      if (activeConversationId.value) await refreshMessages(accessToken)
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : '加载失败，请重试'
      throw reason
    } finally {
      loading.value = false
    }
  }

  async function refreshConversations(accessToken: string): Promise<void> {
    const response = await butlerApi.conversations(accessToken)
    conversations.value = response.items.map(mapConversation)
  }

  async function refreshMessages(
    accessToken: string,
    conversationId = activeConversationId.value,
  ): Promise<void> {
    if (!conversationId) {
      chatItems.value = []
      return
    }
    const response = await butlerApi.messages(conversationId, accessToken)
    // SSE 可以在用户查看另一个历史会话时完成，旧 run 不得覆盖当前时间线。
    if (activeConversationId.value === conversationId) {
      chatItems.value = response.items.flatMap((item: MessageResponse) =>
        mapMessage(item as unknown as ApiObject),
      )
    }
  }

  async function loadConversation(conversationId: string, accessToken: string): Promise<void> {
    stagedScene.value = null
    activeConversationId.value = conversationId
    chatItems.value = []
    await refreshMessages(accessToken, conversationId)
  }

  async function switchAssistantScene(
    target: AssistantSceneTarget,
    accessToken: string,
    options: { cancelExecuting?: boolean } = {},
  ): Promise<AssistantSceneTransition> {
    const viewed = conversations.value.find((item) => item.key === activeConversationId.value)
    if (
      stagedSceneMatches(stagedScene.value, target) ||
      (viewed && conversationMatchesScene(viewed, target))
    ) {
      return 'CURRENT'
    }

    const executing = conversations.value.find(
      (item) => item.runId && executingRunStatuses.includes(item.runStatus ?? ''),
    )
    if (executing && conversationMatchesScene(executing, target)) {
      await loadConversation(executing.key, accessToken)
      return 'RESUMABLE'
    }
    if (executing) {
      if (!options.cancelExecuting) return 'CONFIRMATION_REQUIRED'
      await butlerApi.cancelRun(executing.runId as string, accessToken)
      if (activeRunId.value === executing.runId) activeRunId.value = null
      streamConversationId.value = null
      streamConnection.value?.close()
      streamConnection.value = null
      await refreshConversations(accessToken)
    }

    const resumable = conversations.value.find(
      (item) =>
        conversationMatchesScene(item, target) &&
        suspendedRunStatuses.includes(item.runStatus ?? ''),
    )
    if (resumable) {
      await loadConversation(resumable.key, accessToken)
      return 'RESUMABLE'
    }
    stagedScene.value = target
    activeConversationId.value = null
    chatItems.value = []
    return 'WELCOME'
  }

  async function openSpecialist(
    specialistCode: string,
    accessToken: string,
  ): Promise<AssistantSceneTransition> {
    return switchAssistantScene({ kind: 'SPECIALIST', specialistCode }, accessToken, {
      cancelExecuting: true,
    })
  }

  async function deleteConversation(conversationId: string, accessToken: string): Promise<void> {
    const deletingActiveView = activeConversationId.value === conversationId
    await butlerApi.deleteConversation(conversationId, accessToken)
    await refreshConversations(accessToken)
    if (!deletingActiveView) return

    // 删除正在查看的历史会话后回到服务端唯一 CURRENT 会话。删除接口禁止
    // CURRENT，因此正常情况下始终能找到回退目标；空值分支用于防御损坏响应。
    const current = conversations.value.find((item) => !item.archived)
    activeConversationId.value = current?.key ?? null
    chatItems.value = []
    if (current) await refreshMessages(accessToken, current.key)
  }

  async function sendMessage(
    content: string,
    accessToken: string,
    selection?: { cardId: string; optionId: string },
    attachmentFileIds: string[] = [],
    options: SendMessageOptions = {},
  ): Promise<SendMessageResponse> {
    const conversationId = activeConversationId.value
    const selectedConversation = conversations.value.find((item) => item.key === conversationId)
    const targetConversationId = selectedConversation?.archived ? conversationId : null
    const stagedGeneralWelcome = stagedScene.value?.kind === 'GENERAL'
    const response = await butlerApi.sendMessage(
      {
        schema_version: '1.0',
        client_message_id:
          options.clientMessageId ?? `message-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        target_conversation_id: targetConversationId,
        specialist_code:
          stagedScene.value?.kind === 'SPECIALIST' ? stagedScene.value.specialistCode : null,
        context_policy:
          options.contextPolicy ??
          (targetConversationId
            ? 'CONTINUE_CURRENT'
            : stagedGeneralWelcome
              ? 'ARCHIVE_AND_START'
              : 'AUTO'),
        execution_policy: options.executionPolicy ?? 'REJECT',
        content,
        attachments: attachmentFileIds.map((fileId, position) => ({ file_id: fileId, position })),
        selection: selection
          ? {
              card_id: selection.cardId,
              action_id: 'submit-selection',
              selected_option_ids: [selection.optionId],
            }
          : null,
      },
      accessToken,
    )
    const run = asObject(response.run)
    const stream = asObject(response.stream)
    if (!run || !stream) throw new Error('无效的运行响应')
    const actualConversationId = response.conversation_id
    streamConnection.value?.close()
    streamConnection.value = null
    activeConversationId.value = actualConversationId
    stagedScene.value = null
    activeRunId.value = stringValue(run, 'id')
    streamConversationId.value = actualConversationId
    await Promise.all([
      refreshMessages(accessToken, actualConversationId),
      refreshConversations(accessToken),
    ])
    startStream(
      activeRunId.value,
      actualConversationId,
      stringValue(stream, 'events_url'),
      stringValue(stream, 'ticket'),
      numberValue(stream, 'last_sequence'),
      accessToken,
    )
    return response
  }

  function startStream(
    runId: string,
    conversationId: string,
    eventsUrl: string,
    ticket: string,
    after: number,
    accessToken: string,
  ): void {
    streamConnection.value?.close()
    streamConnection.value = connectRunStream({
      runId,
      eventsUrl,
      ticket,
      after,
      onEvent: async (event) => applyStreamEvent(event, conversationId, accessToken),
      onError: async () => {
        const run = await butlerApi.run(runId, accessToken)
        const status = stringValue(run, 'status')
        if (['SUCCEEDED', 'FAILED_FINAL', 'CANCELLED'].includes(status)) {
          await Promise.all([
            refreshMessages(accessToken, conversationId),
            refreshConversations(accessToken),
          ])
          streamConnection.value?.close()
        }
      },
    })
  }

  async function applyStreamEvent(
    event: RunStreamEvent,
    conversationId: string,
    accessToken: string,
  ): Promise<void> {
    if (event.event === 'progress') {
      if (activeConversationId.value !== conversationId) return
      const code = stringValue(event.payload, 'code')
      const key = `progress-${event.runId}`
      const status: ChatItem = {
        key,
        kind: 'status',
        title: progressLabels[code] ?? '管家正在处理',
        description: '处理完成后会自动显示完整回答。',
        runId: event.runId,
        progressCode: code,
      }
      const index = chatItems.value.findIndex((item) => item.key === key)
      if (index >= 0) chatItems.value[index] = status
      else chatItems.value.push(status)
      return
    }
    if (event.event === 'message.delta') {
      if (activeConversationId.value !== conversationId) return
      const delta = stringValue(event.payload, 'delta')
      const last = [...chatItems.value]
        .reverse()
        .find((item) => item.kind === 'message' && item.role === 'assistant')
      if (last?.kind === 'message') last.content += delta
      return
    }
    if (event.event === 'message.reset') {
      if (activeConversationId.value !== conversationId) return
      const last = [...chatItems.value]
        .reverse()
        .find((item) => item.kind === 'message' && item.role === 'assistant')
      if (last?.kind === 'message') last.content = ''
      return
    }
    if (
      ['message.completed', 'interrupt', 'run.completed', 'run.cancelled'].includes(event.event)
    ) {
      await refreshMessages(accessToken, conversationId)
      if (event.event.startsWith('run.')) {
        activeRunId.value = null
        streamConversationId.value = null
        streamConnection.value?.close()
        const [response] = await Promise.all([
          butlerApi.dashboard(accessToken),
          refreshConversations(accessToken),
        ])
        plans.value = asArray(response.plans).map(mapPlan)
        tasks.value = asArray(response.today_tasks).map(mapTask)
      }
    }
  }

  async function approvePlan(
    item: Extract<ChatItem, { kind: 'plan' }>,
    action: 'APPROVE' | 'EDIT' | 'REJECT',
    accessToken: string,
    feedback?: string,
    executionPolicy: 'REJECT' | 'CANCEL_OTHER' = 'REJECT',
  ): Promise<void> {
    let response: ApiObject
    try {
      response = await butlerApi.approve(
        item.approvalId,
        {
          schema_version: '1.0',
          approval_id: item.approvalId,
          expected_approval_version: item.approvalVersion,
          action,
          feedback: feedback ?? null,
          execution_policy: executionPolicy,
        },
        accessToken,
      )
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'APPROVAL_VERSION_CONFLICT') {
        // 老版本消息可能仍携带旧 approval_version；重新读取服务端投影后再让用户确认。
        await refreshMessages(accessToken)
      }
      throw reason
    }
    item.status = action === 'APPROVE' ? 'approved' : action === 'EDIT' ? 'editing' : 'approved'
    const runId = stringValue(response, 'run_id')
    if (runId) {
      const conversationId = activeConversationId.value
      if (!conversationId) throw new Error('当前没有可继续的对话')
      const ticket = await butlerApi.streamTicket(runId, accessToken)
      startStream(
        runId,
        conversationId,
        stringValue(ticket, 'events_url'),
        stringValue(ticket, 'ticket'),
        numberValue(ticket, 'last_sequence'),
        accessToken,
      )
    }
  }

  async function completeTask(taskId: string, accessToken: string): Promise<void> {
    try {
      await butlerApi.executeTask(
        taskId,
        {
          schema_version: '1.0',
          client_execution_id: `execution-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          result: 'COMPLETED',
          duration_minutes: null,
          feedback: null,
          outcome_data: {},
          occurred_at: new Date().toISOString(),
        },
        accessToken,
      )
      const task = tasks.value.find((item) => item.key === taskId)
      if (task) {
        task.done = true
        task.status = 'DONE'
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'RESOURCE_NOT_FOUND') {
        await load(accessToken)
      }
      throw reason
    }
  }

  function reset(): void {
    streamConnection.value?.close()
    plans.value = []
    tasks.value = []
    chatItems.value = []
    agentShortcuts.value = []
    conversations.value = []
    activeConversationId.value = null
    stagedScene.value = null
    activeRunId.value = null
    streamConversationId.value = null
    error.value = null
  }

  return {
    plans,
    tasks,
    chatItems,
    agentShortcuts,
    conversations,
    activeConversationId,
    stagedScene,
    stagedSpecialistCode,
    loading,
    error,
    activeRunId,
    pendingTasks,
    load,
    switchAssistantScene,
    openSpecialist,
    deleteConversation,
    loadConversation,
    sendMessage,
    approvePlan,
    completeTask,
    refreshMessages,
    refreshConversations,
    reset,
  }
})
