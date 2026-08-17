import type { AgentDefinitionResponse, ApiObject, ConversationResponse } from '@/api/butler'
import type {
  AgentShortcutViewModel,
  ChatItem,
  ConversationViewModel,
  PlanViewModel,
  SourceSummaryViewModel,
  TaskViewModel,
} from '@/types/view-models'

export function asObject(value: unknown): ApiObject | null {
  return typeof value === 'object' && value !== null ? (value as ApiObject) : null
}

export function asArray(value: unknown): ApiObject[] {
  return Array.isArray(value) ? value.map(asObject).filter((item) => item !== null) : []
}

export function stringValue(object: ApiObject, key: string, fallback = ''): string {
  return typeof object[key] === 'string' ? object[key] : fallback
}

export function numberValue(object: ApiObject, key: string, fallback = 0): number {
  return typeof object[key] === 'number' ? object[key] : fallback
}

export function mapPlan(value: ApiObject): PlanViewModel {
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

export function mapTask(value: ApiObject): TaskViewModel {
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

export function mapMessage(value: ApiObject): ChatItem[] {
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

export function mapAgentDefinition(value: AgentDefinitionResponse): AgentShortcutViewModel {
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

export function mapConversation(value: ConversationResponse): ConversationViewModel {
  const runStatus = value.active_run?.status
  const statusLabel =
    runStatus === 'AWAITING_INPUT'
      ? '待回复'
      : runStatus === 'AWAITING_APPROVAL'
        ? '待确认'
        : runStatus === 'FAILED_RETRYABLE'
          ? '待重试'
          : runStatus && ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(runStatus)
            ? '处理中'
            : '已完成'
  return {
    key: value.id,
    title: value.title,
    preview: value.last_message?.content || '开始一个新话题',
    ...conversationTime(value.last_message_at || value.updated_at),
    archived: value.status === 'ARCHIVED',
    agentCode: value.specialist?.code,
    runId: value.active_run?.id,
    runStatus,
    statusLabel,
  }
}
