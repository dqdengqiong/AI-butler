export type MainTab = 'home' | 'plans' | 'chat' | 'mine'

export type SheetName =
  'assistants' | 'attachments' | 'source' | 'settings' | 'materials' | 'history' | null

export type PlanTone = 'blue' | 'purple' | 'green'

export interface PlanViewModel {
  key: string
  icon: string
  title: string
  subtitle: string
  statusLabel: string
  progress: number
  progressLabel: string
  tone: PlanTone
}

export interface TaskViewModel {
  key: string
  planKey: string
  title: string
  planTitle: string
  durationMinutes: number
  done: boolean
  status: 'TODO' | 'DOING' | 'DONE' | 'SKIPPED' | 'CANCELLED'
  tone: PlanTone
}

export interface UploadedAttachment {
  id: string
  name: string
}

/** 服务端能力目录中的公开稳定 code；客户端不得将它替换成内部 user_agent_id。 */
export type AgentShortcutCode = string

export interface AgentStarterPrompt {
  label: string
  content: string
  behavior: 'SEND_MESSAGE' | 'FILL_COMPOSER'
}

/**
 * 聊天输入区展示的专业 Agent 能力投影。
 *
 * availability 来自 agent-definitions API；COMING_SOON 入口只允许展示和提示，
 * 不能发送消息或创建业务对象。
 */
export interface AgentShortcutViewModel {
  code: AgentShortcutCode
  name: string
  icon: string
  description: string
  availability: 'AVAILABLE' | 'COMING_SOON'
  welcomeMessage: string
  starterPrompts: AgentStarterPrompt[]
}

/**
 * 服务端会话列表的展示投影。查看 ARCHIVED 会话不会恢复它；只有发送首条消息时，
 * 服务端才原子切换 CURRENT，客户端随后刷新列表以接受服务端事实。
 */
export interface ConversationViewModel {
  key: string
  title: string
  preview: string
  timeLabel: string
  section: 'today' | 'week' | 'earlier'
  archived: boolean
  agentCode?: AgentShortcutCode
  runId?: string
  runStatus?: string
  statusLabel: '待重试' | '处理中' | '已完成'
}

export interface SourceSummaryViewModel {
  citationId: string
  index: number
  title: string
  domain: string
  sourceType: 'WEB' | 'PRIVATE_FILE' | 'KNOWLEDGE'
  sourceLevel: 'OFFICIAL' | 'GENERAL' | 'PRIVATE'
  publishedAt: string | null
}

export type ChatItem =
  | {
      key: string
      messageId?: string
      kind: 'message'
      role: 'assistant' | 'user'
      content: string
    }
  | {
      key: string
      messageId?: string
      kind: 'planPreview'
      title: string
      description: string
      weeklyMinutes: number
      availableWeeklyMinutes: number
      periodWeeks: 4 | 8 | 12
      startDate: string
      endDate: string
      expiresAt: string
      operation: 'CREATE' | 'ADJUST'
      targetPlanId?: string
      previewHash: string
      warnings: string[]
      status: 'READY' | 'CONFIRMED' | 'SUPERSEDED' | 'DISMISSED' | 'EXPIRED'
      confirming?: boolean
    }
  | {
      key: string
      kind: 'source'
      title: string
      sources: SourceSummaryViewModel[]
      interactive: boolean
    }
  | {
      key: string
      kind: 'status'
      title: string
      description: string
      state?: 'loading' | 'error'
      runId?: string
      attempt?: number
      retryable?: boolean
      retrying?: boolean
      progressCode?: string
    }
