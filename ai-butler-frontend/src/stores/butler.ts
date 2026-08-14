import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  butlerApi,
  type AgentDefinitionResponse,
  type ApiObject,
  type ConversationResponse,
  type MessageResponse,
} from '@/api/butler'
import { ApiError } from '@/api/client'
import { connectRunStream, type RunStreamEvent } from '@/stream/transport'
import type {
  AgentShortcutViewModel,
  ChatItem,
  ConversationViewModel,
  PlanViewModel,
  SourceSummaryViewModel,
  TaskViewModel,
} from '@/types/view-models'

const progressLabels: Record<string, string> = {
  SEARCHING_WEB: '正在检索网络',
  RETRIEVING_PRIVATE: '正在检索我的资料',
  ORGANIZING_CITATIONS: '正在整理引用',
  GENERATING_ANSWER: '正在生成回答',
}

function asObject(value: unknown): ApiObject | null {
  return typeof value === 'object' && value !== null ? (value as ApiObject) : null
}

function asArray(value: unknown): ApiObject[] {
  return Array.isArray(value) ? value.map(asObject).filter((item) => item !== null) : []
}

function stringValue(object: ApiObject, key: string, fallback = ''): string {
  return typeof object[key] === 'string' ? object[key] : fallback
}

function numberValue(object: ApiObject, key: string, fallback = 0): number {
  return typeof object[key] === 'number' ? object[key] : fallback
}

function mapPlan(value: ApiObject): PlanViewModel {
  const progress = asObject(value.progress) ?? {}
  const completed = numberValue(progress, 'completed')
  const total = numberValue(progress, 'total')
  return {
    key: stringValue(value, 'id'),
    icon: '公',
    title: stringValue(value, 'title', '公务员备考'),
    subtitle: '当前已批准版本',
    statusLabel: stringValue(value, 'status', 'ACTIVE') === 'ACTIVE' ? '进行中' : '草案',
    progress: numberValue(progress, 'percent'),
    progressLabel: `${completed} / ${total} 项`,
    tone: 'blue',
  }
}

function mapTask(value: ApiObject): TaskViewModel {
  const status = stringValue(value, 'status', 'TODO') as TaskViewModel['status']
  return {
    key: stringValue(value, 'id'),
    planKey: stringValue(value, 'plan_id'),
    title: stringValue(value, 'title'),
    planTitle: stringValue(value, 'plan_title', '公务员备考'),
    durationMinutes: numberValue(value, 'expected_minutes'),
    done: status === 'DONE',
    status,
    tone: 'blue',
  }
}

function mapCard(value: ApiObject): ChatItem | null {
  const type = stringValue(value, 'card_type')
  const payload = asObject(value.payload) ?? {}
  const refs = asObject(value.entity_refs) ?? {}
  if (type === 'PlanCard') {
    const approvalStatus = stringValue(refs, 'approval_status', 'PENDING')
    return {
      key: stringValue(value, 'card_id'),
      kind: 'plan',
      title: stringValue(payload, 'title', '公务员备考计划'),
      description: stringValue(payload, 'objective_summary'),
      weeklyMinutes: numberValue(payload, 'weekly_minutes'),
      status:
        approvalStatus === 'PENDING'
          ? 'pending'
          : approvalStatus === 'EDITED'
            ? 'editing'
            : 'approved',
      approvalId: stringValue(refs, 'approval_id'),
      approvalVersion: numberValue(refs, 'approval_version', 1),
    }
  }
  if (type === 'SourceCard') {
    const supported = stringValue(value, 'schema_version') === '1.0'
    const sources: SourceSummaryViewModel[] = supported
      ? asArray(payload.sources).map((item) => ({
          citationId: stringValue(item, 'citation_id'),
          index: numberValue(item, 'index'),
          title: stringValue(item, 'title', '引用来源'),
          domain: stringValue(item, 'domain', '来源信息不可用'),
          sourceType: ['WEB', 'PRIVATE_FILE', 'KNOWLEDGE'].includes(
            stringValue(item, 'source_type'),
          )
            ? (stringValue(item, 'source_type') as SourceSummaryViewModel['sourceType'])
            : 'KNOWLEDGE',
          sourceLevel: ['OFFICIAL', 'GENERAL', 'PRIVATE'].includes(
            stringValue(item, 'source_level'),
          )
            ? (stringValue(item, 'source_level') as SourceSummaryViewModel['sourceLevel'])
            : 'GENERAL',
          publishedAt: typeof item.published_at === 'string' ? item.published_at : null,
        }))
      : []
    return {
      key: stringValue(value, 'card_id'),
      kind: 'source',
      title: supported ? stringValue(payload, 'title', '引用来源') : '当前引用卡版本暂不支持',
      sources,
      interactive: supported,
    }
  }
  if (type === 'SelectionCard') {
    const options = asArray(payload.options)
    const actions = asArray(value.actions)
    const submittedOptionIds = Array.isArray(payload.submitted_option_ids)
      ? payload.submitted_option_ids.filter((item): item is string => typeof item === 'string')
      : []
    const selectedIndex = options.findIndex((item) =>
      submittedOptionIds.includes(stringValue(item, 'id')),
    )
    const allowFreeText = stringValue(payload, 'input_mode') === 'NATURAL_LANGUAGE'
    return {
      key: stringValue(value, 'card_id'),
      kind: 'selection',
      title: stringValue(payload, 'question', '请选择'),
      description: stringValue(payload, 'description', '选择会作为上下文提交，不会直接修改计划。'),
      options: options.map((item) => stringValue(item, 'label')),
      optionIds: options.map((item) => stringValue(item, 'id')),
      selected: selectedIndex >= 0 ? selectedIndex : allowFreeText ? -1 : 0,
      submitted: payload.submitted === true,
      cardId: stringValue(value, 'card_id'),
      allowFreeText,
      inputPlaceholder: stringValue(payload, 'input_placeholder'),
      submitLabel: actions[0] ? stringValue(actions[0], 'label', '确认选择') : '确认选择',
    }
  }
  if (type === 'StatusCard') {
    return {
      key: stringValue(value, 'card_id'),
      kind: 'status',
      title: stringValue(payload, 'title', '管家正在处理'),
      description: stringValue(payload, 'description'),
    }
  }
  return null
}

function mapMessage(value: ApiObject): ChatItem[] {
  const message: ChatItem = {
    key: stringValue(value, 'id'),
    messageId: stringValue(value, 'id'),
    kind: 'message',
    role: stringValue(value, 'role') === 'USER' ? 'user' : 'assistant',
    content: stringValue(value, 'content'),
  }
  const structured = asObject(value.cards)
  const cards = structured ? asArray(structured.cards).map(mapCard).filter(Boolean) : []
  return [message, ...(cards as ChatItem[])]
}

function uuidV4(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (character) => {
    const random = Math.floor(Math.random() * 16)
    const value = character === 'x' ? random : (random & 0x3) | 0x8
    return value.toString(16)
  })
}

function mapAgentDefinition(value: AgentDefinitionResponse): AgentShortcutViewModel {
  return {
    code: value.code,
    name: value.name,
    icon: value.icon,
    description: value.description,
    availability: value.availability,
    welcomeMessage: value.welcome_message,
    starterPrompts: value.starter_prompts,
  }
}

function conversationTime(
  value: string | null,
): Pick<ConversationViewModel, 'timeLabel' | 'section'> {
  const date = value ? new Date(value) : new Date()
  const now = new Date()
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const ageInDays = Math.floor((startToday.getTime() - date.getTime()) / 86_400_000)
  if (ageInDays <= 0) {
    return {
      timeLabel: `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`,
      section: 'today',
    }
  }
  if (ageInDays < 7) {
    return { timeLabel: `周${'日一二三四五六'[date.getDay()]}`, section: 'week' }
  }
  return { timeLabel: `${date.getMonth() + 1} 月 ${date.getDate()} 日`, section: 'earlier' }
}

function mapConversation(value: ConversationResponse): ConversationViewModel {
  return {
    key: value.id,
    title: value.title,
    preview: value.last_message?.content || '开始一个新话题',
    ...conversationTime(value.last_message_at || value.updated_at),
    archived: value.status === 'ARCHIVED',
    agentCode: value.specialist?.code,
  }
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
    activeConversationId.value = conversationId
    chatItems.value = []
    await refreshMessages(accessToken, conversationId)
  }

  async function createConversation(accessToken: string, specialistCode?: string): Promise<void> {
    const conversation = await butlerApi.createConversation(
      {
        schema_version: '1.0',
        client_conversation_id: uuidV4(),
        specialist_code: specialistCode ?? null,
      },
      accessToken,
    )
    await refreshConversations(accessToken)
    await loadConversation(conversation.id, accessToken)
  }

  async function sendMessage(
    content: string,
    accessToken: string,
    selection?: { cardId: string; optionId: string },
    attachmentFileIds: string[] = [],
  ): Promise<void> {
    const conversationId = activeConversationId.value
    if (!conversationId) throw new Error('当前没有可发送的对话')
    const response = await butlerApi.sendMessage(
      conversationId,
      {
        schema_version: '1.0',
        client_message_id: `message-${Date.now()}-${Math.random().toString(36).slice(2)}`,
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
    activeRunId.value = stringValue(run, 'id')
    streamConversationId.value = conversationId
    await Promise.all([
      refreshMessages(accessToken, conversationId),
      refreshConversations(accessToken),
    ])
    startStream(
      activeRunId.value,
      conversationId,
      stringValue(stream, 'events_url'),
      stringValue(stream, 'ticket'),
      numberValue(stream, 'last_sequence'),
      accessToken,
    )
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
    loading,
    error,
    activeRunId,
    pendingTasks,
    load,
    createConversation,
    loadConversation,
    sendMessage,
    approvePlan,
    completeTask,
    refreshMessages,
    refreshConversations,
    reset,
  }
})
