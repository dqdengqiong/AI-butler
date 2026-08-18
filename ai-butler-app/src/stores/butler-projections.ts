import type { AgentDefinitionResponse, ApiObject, ConversationResponse } from '@/api/butler'
import type {
  AgentShortcutViewModel,
  ChatItem,
  ConversationViewModel,
  PlanViewModel,
  SourceSummaryViewModel,
  TaskViewModel,
} from '@/types/view-models'

/** 将服务端公开进度码映射为可展示文案，未知码由 Store 使用通用文案降级。 */
export const progressLabels: Readonly<Record<string, string>> = {
  UNDERSTANDING_INTENT: '正在理解你的意图',
  RESEARCHING: '正在研究相关资料',
  SEARCHING_WEB: '正在检索网络',
  RETRIEVING_PRIVATE: '正在检索我的资料',
  ORGANIZING_CITATIONS: '正在整理引用',
  GENERATING_ANSWER: '正在生成回答',
  BUILDING_PLAN: '正在制定计划',
  REVIEWING_PLAN: '正在校验计划',
  SCHEDULING_TASKS: '正在安排未来 7 天任务',
  GENERATING_RESPONSE: '正在生成回复',
}

/** 将稳定错误码转换为明确失败语义，避免模型失败被误解为已创建计划。 */
export function runErrorPresentation(
  code: string,
  retryable: boolean,
): { title: string; description: string } {
  if (code === 'PLANNER_MODEL_UNAVAILABLE') {
    return {
      title: '计划生成超时',
      description: '计划生成超时，本次未创建计划，可以重试。',
    }
  }
  if (code === 'PLANNER_MODEL_INVALID' || code.startsWith('PLAN_')) {
    return {
      title: '计划草稿校验失败',
      description: '计划草稿未通过校验，本次未创建计划。',
    }
  }
  return {
    title: retryable ? '暂时无法生成回答' : '回答生成失败',
    description: retryable ? '上次处理未完成，可以重新生成。' : '本次处理未完成。',
  }
}

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
    const schemaVersion = stringValue(value, 'schema_version')
    if (schemaVersion !== '1.0' && schemaVersion !== '1.1') {
      return {
        key: stringValue(value, 'card_id'),
        kind: 'status',
        title: '当前计划卡版本暂不支持',
        description: '请刷新客户端或在计划页查看服务端已保存的计划。',
      }
    }
    const approvalStatus = stringValue(refs, 'approval_status', 'PENDING')
    const mode = stringValue(payload, 'mode', 'SINGLE_PLAN_ADJUST')
    const plans =
      schemaVersion === '1.1'
        ? asArray(payload.plans).map((plan, index) => ({
            key: stringValue(plan, 'work_item_id', `plan-${index}`),
            title: stringValue(plan, 'title', `计划 ${index + 1}`),
            description: stringValue(plan, 'objective_summary'),
            weeklyMinutes: numberValue(plan, 'weekly_minutes'),
            startDate: stringValue(plan, 'start_date') || undefined,
            endDate: stringValue(plan, 'end_date') || undefined,
          }))
        : [
            {
              key: stringValue(value, 'card_id'),
              title: stringValue(payload, 'title', '公务员备考计划'),
              description: stringValue(payload, 'objective_summary'),
              weeklyMinutes: numberValue(payload, 'weekly_minutes'),
              startDate: stringValue(payload, 'start_date') || undefined,
              endDate: stringValue(payload, 'end_date') || undefined,
            },
          ]
    // 1.1 的 cardinality 是副作用审批契约的一部分；异常卡片必须只读降级。
    if (
      schemaVersion === '1.1' &&
      ((mode === 'BUNDLE_CREATE' && plans.length < 2) ||
        (mode === 'SINGLE_PLAN_ADJUST' && plans.length !== 1))
    ) {
      return {
        key: stringValue(value, 'card_id'),
        kind: 'status',
        title: '计划草案结构无效',
        description: '此卡片不会执行批准操作，请重新生成计划。',
      }
    }
    return {
      key: stringValue(value, 'card_id'),
      kind: 'plan',
      schemaVersion,
      mode,
      title: stringValue(payload, 'title', '公务员备考计划'),
      description:
        schemaVersion === '1.0'
          ? stringValue(payload, 'objective_summary')
          : `${plans.length} 个独立计划，共 ${numberValue(payload, 'total_weekly_minutes')} 分钟/周`,
      weeklyMinutes:
        schemaVersion === '1.0'
          ? numberValue(payload, 'weekly_minutes')
          : numberValue(payload, 'total_weekly_minutes'),
      plans,
      warnings: Array.isArray(payload.warnings)
        ? payload.warnings.filter((item): item is string => typeof item === 'string')
        : [],
      status:
        approvalStatus === 'PENDING'
          ? 'pending'
          : approvalStatus === 'EDITED'
            ? 'editing'
            : approvalStatus === 'REJECTED'
              ? 'rejected'
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
