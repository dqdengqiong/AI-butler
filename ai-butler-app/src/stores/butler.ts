import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { butlerApi, type SendMessageResponse } from '@/api/butler'
import { ApiError } from '@/api/client'
import { connectRunStream, type RunStreamEvent } from '@/stream/transport'
import { createRunRetry } from '@/stores/butler-run-retry'
import { refreshConversationMessages } from '@/stores/butler-message-refresh'
import {
  asArray,
  asObject,
  mapAgentDefinition,
  mapConversation,
  mapPlan,
  mapTask,
  numberValue,
  progressLabels,
  runErrorPresentation,
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
  const streamConnection = ref<{ close(): void } | null>(null)
  const streamMessageIds = new Map<string, string>()

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
    return refreshConversationMessages(accessToken, conversationId, {
      activeConversationId,
      conversations,
      chatItems,
    })
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
      streamConnection.value?.close()
      streamConnection.value = null
      await refreshConversations(accessToken)
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

    const current = conversations.value.find((item) => !item.archived)
    activeConversationId.value = current?.key ?? null
    chatItems.value = []
    if (current) await refreshMessages(accessToken, current.key)
  }

  async function sendMessage(
    content: string,
    accessToken: string,
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

  const retryState = { conversations, activeConversationId, chatItems, activeRunId }
  const retryRun = createRunRetry(retryState, refreshConversations, startStream)

  async function applyStreamEvent(
    event: RunStreamEvent,
    conversationId: string,
    accessToken: string,
  ): Promise<void> {
    if (event.event === 'message.start') {
      if (activeConversationId.value !== conversationId) return
      const messageId = stringValue(event.payload, 'message_id')
      if (messageId) streamMessageIds.set(event.runId, messageId)
      // 消息查询通常已返回空的 Assistant 占位；若 start 先于查询结果到达，
      // 仍需创建临时投影，保证后续 delta 有明确归属而不污染上一轮回答。
      if (
        messageId &&
        !chatItems.value.some((item) => item.kind === 'message' && item.messageId === messageId)
      ) {
        chatItems.value.push({
          key: messageId,
          messageId,
          kind: 'message',
          role: 'assistant',
          content: '',
        })
      }
      return
    }
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
      const messageId = streamMessageIds.get(event.runId)
      let target = chatItems.value.find(
        (item) => item.kind === 'message' && item.messageId === messageId,
      )
      if (!target) {
        // 断线续传可能从 delta 开始，message.start 已在 after 游标之前。使用 run
        // 级临时 key，完成事件到达后再由服务端消息投影整体覆盖。
        const key = `stream-message-${event.runId}`
        target = chatItems.value.find((item) => item.key === key)
        if (!target) {
          target = { key, kind: 'message', role: 'assistant', content: '' }
          chatItems.value.push(target)
        }
      }
      if (target.kind === 'message') target.content += delta
      return
    }
    if (event.event === 'message.reset') {
      if (activeConversationId.value !== conversationId) return
      const messageId = streamMessageIds.get(event.runId)
      const target = chatItems.value.find(
        (item) =>
          item.kind === 'message' &&
          (item.messageId === messageId || item.key === `stream-message-${event.runId}`),
      )
      if (target?.kind === 'message') target.content = ''
      return
    }
    if (event.event === 'error') {
      await Promise.all([
        refreshMessages(accessToken, conversationId),
        refreshConversations(accessToken),
      ])
      if (activeConversationId.value === conversationId) {
        const retryable = event.payload.retryable === true
        const errorCode = stringValue(event.payload, 'code')
        const copy = runErrorPresentation(errorCode, retryable)
        const key = `progress-${event.runId}`
        const failure: ChatItem = {
          key,
          kind: 'status',
          state: 'error',
          title: copy.title,
          description:
            errorCode.startsWith('PLANNER_MODEL_') || errorCode.startsWith('PLAN_')
              ? copy.description
              : stringValue(event.payload, 'message', copy.description),
          runId: event.runId,
        }
        Object.assign(failure, { attempt: event.attempt, retryable })
        const index = chatItems.value.findIndex((item) => item.key === key)
        if (index >= 0) chatItems.value[index] = failure
        else chatItems.value.push(failure)
      }
      streamMessageIds.delete(event.runId)
      streamConnection.value?.close()
      streamConnection.value = null
      return
    }
    if (['message.completed', 'run.completed', 'run.cancelled'].includes(event.event)) {
      await refreshMessages(accessToken, conversationId)
      streamMessageIds.delete(event.runId)
      if (event.event.startsWith('run.')) {
        activeRunId.value = null
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

  async function confirmPlanPreview(
    item: Extract<ChatItem, { kind: 'planPreview' }>,
    accessToken: string,
  ): Promise<void> {
    if (item.status !== 'READY' || item.confirming || !item.messageId) return
    item.confirming = true
    try {
      await butlerApi.confirmPlanPreview(
        item.messageId,
        { schema_version: '1.0', expected_preview_hash: item.previewHash },
        accessToken,
      )
      item.status = 'CONFIRMED'
      const [dashboard] = await Promise.all([
        butlerApi.dashboard(accessToken),
        refreshMessages(accessToken),
      ])
      plans.value = asArray(dashboard.plans).map(mapPlan)
      tasks.value = asArray(dashboard.today_tasks).map(mapTask)
    } finally {
      item.confirming = false
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

  async function deletePlan(planId: string, accessToken: string): Promise<void> {
    await butlerApi.deletePlan(planId, accessToken)
    const dashboard = await butlerApi.dashboard(accessToken)
    plans.value = asArray(dashboard.plans).map(mapPlan)
    tasks.value = asArray(dashboard.today_tasks).map(mapTask)
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
    streamMessageIds.clear()
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
    retryRun,
    confirmPlanPreview,
    completeTask,
    deletePlan,
    refreshMessages,
    refreshConversations,
    reset,
  }
})
