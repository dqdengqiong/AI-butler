import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  dashboard: vi.fn(),
  agentDefinitions: vi.fn(),
  conversations: vi.fn(),
  deleteConversation: vi.fn(),
  messages: vi.fn(),
  sendMessage: vi.fn(),
  run: vi.fn(),
  cancelRun: vi.fn(),
  retryRun: vi.fn(),
  approve: vi.fn(),
  streamTicket: vi.fn(),
  executeTask: vi.fn(),
}))
const stream = vi.hoisted(() => ({
  options: null as Record<string, unknown> | null,
  close: vi.fn(),
}))
vi.mock('@/api/butler', () => ({ butlerApi: api }))
vi.mock('@/stream/transport', () => ({
  connectRunStream: vi.fn((options: Record<string, unknown>) => {
    stream.options = options
    return { close: stream.close }
  }),
}))

import { ApiError } from '@/api/client'
import { useButlerStore } from '@/stores/butler'

const dashboard = {
  plans: [
    {
      id: 'plan-1',
      title: '省考计划',
      status: 'ACTIVE',
      progress: { completed: 1, total: 2, percent: 50 },
    },
  ],
  today_tasks: [
    {
      id: 'task-1',
      plan_id: 'plan-1',
      title: '做行测题',
      plan_title: '省考计划',
      expected_minutes: 30,
      status: 'TODO',
    },
  ],
}

const messages = {
  items: [
    { id: 'm-user', role: 'USER', content: '我要备考', cards: null },
    {
      id: 'm-agent',
      role: 'ASSISTANT',
      content: '请选择并审批。',
      cards: {
        cards: [
          {
            schema_version: '1.0',
            card_id: 'card-plan',
            card_type: 'PlanCard',
            payload: { title: '省考计划', objective_summary: '每周六小时', weekly_minutes: 360 },
            entity_refs: {
              approval_id: 'approval-1',
              approval_version: 2,
              approval_status: 'PENDING',
            },
          },
          {
            schema_version: '1.0',
            card_id: 'card-source',
            card_type: 'SourceCard',
            payload: {
              title: '引用来源',
              sources: [
                {
                  citation_id: 'citation-1',
                  index: 1,
                  title: '考试公告',
                  domain: 'gov.example',
                  source_type: 'WEB',
                  source_level: 'OFFICIAL',
                  published_at: '2026-08-01T00:00:00Z',
                },
              ],
            },
            entity_refs: { citation_ids: ['citation-1'] },
          },
          {
            schema_version: '2.0',
            card_id: 'card-source-future',
            card_type: 'SourceCard',
            payload: { title: '未来引用', sources: [{ citation_id: 'unsafe' }] },
            entity_refs: {},
          },
          {
            card_id: 'card-selection',
            card_type: 'SelectionCard',
            payload: {
              question: '学习时间？',
              description: '可以直接描述，也可以选择常用安排。',
              input_mode: 'NATURAL_LANGUAGE',
              input_placeholder: '例如：每天 1 小时，周末不学习',
              options: [{ id: 'six', label: '6 小时' }],
            },
            actions: [{ action_id: 'submit-selection', label: '确认选择' }],
            entity_refs: {},
          },
          {
            card_id: 'card-status',
            card_type: 'StatusCard',
            payload: { title: '处理中', description: '正在检索' },
            entity_refs: {},
          },
          { card_id: 'unknown', card_type: 'FutureCard', payload: {}, entity_refs: {} },
        ],
      },
    },
  ],
}

const conversations = {
  items: [
    {
      id: 'conversation-1',
      title: '新的对话',
      status: 'CURRENT',
      specialist: null,
      last_message: null,
      last_message_at: null,
      active_run: null,
      created_at: '2026-08-13T00:00:00Z',
      updated_at: '2026-08-13T00:00:00Z',
    },
  ],
  next_cursor: null,
  has_more: false,
}

describe('server-fact butler store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    stream.options = null
    stream.close.mockReset()
    api.dashboard.mockReset().mockResolvedValue(dashboard)
    api.agentDefinitions.mockReset().mockResolvedValue({
      items: [
        {
          code: 'CIVIL_SERVICE_EXAM',
          name: '考公',
          icon: '公',
          description: '规划备考',
          availability: 'AVAILABLE',
          welcome_message: '欢迎',
          starter_prompts: [],
        },
      ],
    })
    api.conversations.mockReset().mockResolvedValue(conversations)
    api.deleteConversation.mockReset().mockResolvedValue(undefined)
    api.messages.mockReset().mockResolvedValue(messages)
    api.sendMessage.mockReset().mockResolvedValue({
      conversation_id: 'conversation-1',
      transition: { kind: 'CONTINUED', archived_conversation_id: null },
      run: { id: 'run-1' },
      stream: { events_url: '/v1/events', ticket: 'ticket', last_sequence: 0 },
    })
    api.run.mockReset().mockResolvedValue({ status: 'SUCCEEDED' })
    api.cancelRun.mockReset().mockResolvedValue({ status: 'CANCELLED' })
    api.retryRun.mockReset().mockResolvedValue({ status: 'QUEUED', attempt: 1 })
    api.approve.mockReset().mockResolvedValue({ run_id: 'run-2' })
    api.streamTicket.mockReset().mockResolvedValue({
      events_url: '/v1/events-2',
      ticket: 'ticket-2',
      last_sequence: 3,
    })
    api.executeTask.mockReset().mockResolvedValue({})
  })

  it('maps dashboard, messages and all safe card versions', async () => {
    const store = useButlerStore()
    await store.load('access')
    expect(store.plans[0]?.progress).toBe(50)
    expect(store.tasks[0]?.done).toBe(false)
    expect(store.chatItems.map((item) => item.kind)).toEqual([
      'message',
      'message',
      'plan',
      'source',
      'source',
      'selection',
      'status',
    ])
    expect(store.pendingTasks).toHaveLength(1)
    expect(store.agentShortcuts[0]?.code).toBe('CIVIL_SERVICE_EXAM')
    expect(store.conversations[0]?.archived).toBe(false)
    expect(store.chatItems.find((item) => item.kind === 'plan')).toMatchObject({
      approvalVersion: 2,
      weeklyMinutes: 360,
      status: 'pending',
    })
    const sources = store.chatItems.filter((item) => item.kind === 'source')
    expect(sources[0]).toMatchObject({ interactive: true, sources: [{ citationId: 'citation-1' }] })
    expect(sources[1]).toMatchObject({ interactive: false, sources: [] })
    expect(store.chatItems.find((item) => item.kind === 'selection')).toMatchObject({
      allowFreeText: true,
      selected: -1,
      inputPlaceholder: '例如：每天 1 小时，周末不学习',
    })
  })

  it('sends selections and attachments, then reconciles stream events', async () => {
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('目标', 'access', { cardId: 'card-selection', optionId: 'six' }, [
      'file-1',
    ])
    expect(api.sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        target_conversation_id: null,
        context_policy: 'AUTO',
        attachments: [{ file_id: 'file-1', position: 0 }],
        selection: expect.objectContaining({ selected_option_ids: ['six'] }),
      }),
      'access',
    )
    const onEvent = stream.options?.onEvent as (event: Record<string, unknown>) => Promise<void>
    await onEvent({
      event: 'progress',
      runId: 'run-1',
      payload: { code: 'ORGANIZING_CITATIONS' },
    })
    expect(store.chatItems.at(-1)).toMatchObject({
      kind: 'status',
      title: '正在整理引用',
    })
    await onEvent({
      event: 'message.start',
      runId: 'run-1',
      payload: { message_id: 'stream-assistant' },
    })
    await onEvent({ event: 'message.delta', runId: 'run-1', payload: { delta: '增量' } })
    expect(
      store.chatItems.find(
        (item) => item.kind === 'message' && item.messageId === 'stream-assistant',
      ),
    ).toMatchObject({ content: '增量' })
    await onEvent({ event: 'message.reset', runId: 'run-1', payload: {} })
    await onEvent({ event: 'message.completed', runId: 'run-1', payload: {} })
    await onEvent({ event: 'run.completed', runId: 'run-1', payload: {} })
    expect(store.activeRunId).toBeNull()
    expect(api.dashboard).toHaveBeenCalledTimes(2)
  })

  it('shows a terminal error instead of leaving the progress card spinning', async () => {
    api.messages.mockResolvedValue({
      items: [
        { id: 'm-user', role: 'USER', content: '问题', cards: null },
        { id: 'm-pending', role: 'ASSISTANT', content: '', cards: null },
      ],
    })
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('问题', 'access')
    const onEvent = stream.options?.onEvent as (event: Record<string, unknown>) => Promise<void>

    await onEvent({
      event: 'progress',
      runId: 'run-1',
      payload: { code: 'GENERATING_ANSWER' },
    })
    await onEvent({
      event: 'error',
      runId: 'run-1',
      attempt: 0,
      payload: {
        code: 'RAG_MODEL_UNAVAILABLE',
        message: '回答生成暂时不可用，请稍后重试',
        retryable: true,
      },
    })

    expect(store.chatItems.at(-1)).toMatchObject({
      kind: 'status',
      state: 'error',
      title: '暂时无法生成回答',
      description: '回答生成暂时不可用，请稍后重试',
      attempt: 0,
      retryable: true,
    })
    expect(stream.close).toHaveBeenCalled()
    expect(api.conversations).toHaveBeenCalledTimes(3)
  })

  it('retries a failed run with its expected attempt and reconnects the stream', async () => {
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('问题', 'access')
    const onEvent = stream.options?.onEvent as (event: Record<string, unknown>) => Promise<void>
    await onEvent({
      event: 'error',
      runId: 'run-1',
      attempt: 0,
      payload: { message: '暂时不可用', retryable: true },
    })

    await store.retryRun('run-1', 0, 'access')

    expect(api.retryRun).toHaveBeenCalledWith(
      'run-1',
      {
        schema_version: '1.0',
        expected_attempt: 0,
        execution_policy: 'REJECT',
      },
      'access',
    )
    expect(api.streamTicket).toHaveBeenCalledWith('run-1', 'access')
    expect(store.chatItems.at(-1)).toMatchObject({
      kind: 'status',
      state: 'loading',
      title: '正在重新生成',
      runId: 'run-1',
    })
    expect(stream.options).toMatchObject({ runId: 'run-1', after: 3 })
  })

  it('reloads the attempt before retrying a legacy error card', async () => {
    api.run.mockResolvedValue({
      status: 'FAILED_RETRYABLE',
      attempt: 2,
      error: { code: 'MODEL_UNAVAILABLE', retryable: true },
    })
    const store = useButlerStore()
    await store.load('access')

    await store.retryRun('run-1', undefined, 'access')

    expect(api.run).toHaveBeenCalledWith('run-1', 'access')
    expect(api.retryRun).toHaveBeenCalledWith(
      'run-1',
      expect.objectContaining({ expected_attempt: 2 }),
      'access',
    )
  })

  it('restores a retryable error card after reloading the conversation', async () => {
    api.conversations.mockResolvedValue({
      ...conversations,
      items: [
        {
          ...conversations.items[0],
          active_run: { id: 'run-failed', status: 'FAILED_RETRYABLE' },
        },
      ],
    })
    api.run.mockResolvedValue({
      status: 'FAILED_RETRYABLE',
      attempt: 2,
      error: { code: 'PLANNER_MODEL_UNAVAILABLE', retryable: true },
    })
    const store = useButlerStore()

    await store.load('access')

    expect(api.run).toHaveBeenCalledWith('run-failed', 'access')
    expect(store.chatItems.at(-1)).toMatchObject({
      kind: 'status',
      state: 'error',
      title: '计划生成超时',
      description: '计划生成超时，本次未创建计划，可以重试。',
      runId: 'run-failed',
      attempt: 2,
      retryable: true,
    })
  })

  it('creates a run-scoped assistant projection when replay starts at a delta', async () => {
    api.messages.mockResolvedValue({
      items: [{ id: 'm-user', role: 'USER', content: '问题', cards: null }],
    })
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('问题', 'access')
    const onEvent = stream.options?.onEvent as (event: Record<string, unknown>) => Promise<void>

    await onEvent({ event: 'message.delta', runId: 'run-1', payload: { delta: '第一段' } })
    await onEvent({ event: 'message.delta', runId: 'run-1', payload: { delta: '第二段' } })

    expect(store.chatItems.at(-1)).toMatchObject({
      key: 'stream-message-run-1',
      kind: 'message',
      role: 'assistant',
      content: '第一段第二段',
    })
  })

  it('compensates a disconnected terminal run and replaces prior stream', async () => {
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('开始', 'access')
    await store.sendMessage('继续', 'access')
    expect(stream.close).toHaveBeenCalled()
    const onError = stream.options?.onError as () => Promise<void>
    await onError()
    expect(api.run).toHaveBeenCalled()
    expect(api.messages).toHaveBeenCalled()
  })

  it('stages a specialist without creating an empty conversation and switches on first send', async () => {
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('开始', 'access')
    const nextConversation = {
      ...conversations.items[0],
      id: 'conversation-2',
      status: 'CURRENT',
      specialist: { code: 'CIVIL_SERVICE_EXAM', name: '考公', icon: '公' },
    }
    api.sendMessage.mockResolvedValueOnce({
      conversation_id: 'conversation-2',
      transition: { kind: 'CREATED', archived_conversation_id: 'conversation-1' },
      run: { id: 'run-2' },
      stream: { events_url: '/v1/events-2', ticket: 'ticket-2', last_sequence: 0 },
    })
    api.conversations.mockResolvedValue({
      items: [nextConversation, { ...conversations.items[0], status: 'ARCHIVED' }],
      next_cursor: null,
      has_more: false,
    })
    api.messages.mockResolvedValueOnce({ items: [], next_cursor: null, has_more: false })

    await expect(store.openSpecialist('CIVIL_SERVICE_EXAM', 'access')).resolves.toBe('WELCOME')
    expect(store.activeConversationId).toBeNull()
    expect(store.chatItems).toEqual([])
    await store.sendMessage('制定备考计划', 'access')

    expect(stream.close).toHaveBeenCalledTimes(1)
    expect(api.sendMessage).toHaveBeenLastCalledWith(
      expect.objectContaining({ specialist_code: 'CIVIL_SERVICE_EXAM' }),
      'access',
    )
    expect(store.activeRunId).toBe('run-2')
    expect(store.activeConversationId).toBe('conversation-2')
  })

  it('preserves the old stream when a staged specialist message fails', async () => {
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('开始', 'access')
    await store.openSpecialist('CIVIL_SERVICE_EXAM', 'access')
    api.sendMessage.mockRejectedValueOnce(new Error('offline'))

    await expect(store.sendMessage('制定计划', 'access')).rejects.toThrow('offline')

    expect(stream.close).not.toHaveBeenCalled()
    expect(store.activeRunId).toBe('run-1')
    expect(store.activeConversationId).toBeNull()
    expect(store.stagedSpecialistCode).toBe('CIVIL_SERVICE_EXAM')
  })

  it('falls back to the current conversation after deleting the viewed history', async () => {
    const current = conversations.items[0]
    const archived = {
      ...current,
      id: 'conversation-history',
      title: '历史对话',
      status: 'ARCHIVED',
    }
    api.conversations.mockResolvedValueOnce({
      items: [current, archived],
      next_cursor: null,
      has_more: false,
    })
    const store = useButlerStore()
    await store.load('access')
    await store.loadConversation('conversation-history', 'access')
    api.conversations.mockResolvedValueOnce({
      items: [current],
      next_cursor: null,
      has_more: false,
    })

    await store.deleteConversation('conversation-history', 'access')

    expect(api.deleteConversation).toHaveBeenCalledWith('conversation-history', 'access')
    expect(store.activeConversationId).toBe('conversation-1')
    expect(store.conversations).toHaveLength(1)
  })

  it('maps workflow labels and resumes the latest suspended specialist', async () => {
    const current = conversations.items[0]
    const specialist = {
      ...current,
      id: 'conversation-specialist',
      status: 'ARCHIVED',
      specialist: { code: 'CIVIL_SERVICE_EXAM', name: '考公', icon: '公' },
      active_run: { id: 'run-waiting', status: 'AWAITING_INPUT' },
    }
    api.conversations.mockResolvedValueOnce({
      items: [current, specialist],
      next_cursor: null,
      has_more: false,
    })
    const store = useButlerStore()
    await store.load('access')

    expect(store.conversations.map((item) => item.statusLabel)).toEqual(['已完成', '待回复'])
    await expect(store.openSpecialist('CIVIL_SERVICE_EXAM', 'access')).resolves.toBe('RESUMABLE')
    expect(store.activeConversationId).toBe('conversation-specialist')

    api.sendMessage.mockResolvedValueOnce({
      conversation_id: 'conversation-specialist',
      transition: { kind: 'RESUMED', archived_conversation_id: 'conversation-1' },
      run: { id: 'run-waiting' },
      stream: { events_url: '/v1/events', ticket: 'ticket', last_sequence: 4 },
    })
    await store.sendMessage('继续', 'access')
    expect(api.sendMessage).toHaveBeenLastCalledWith(
      expect.objectContaining({
        target_conversation_id: 'conversation-specialist',
        context_policy: 'CONTINUE_CURRENT',
      }),
      'access',
    )
  })

  it('cancels a running current task when entering another specialist welcome state', async () => {
    const running = {
      ...conversations.items[0],
      active_run: { id: 'run-current', status: 'RUNNING' },
    }
    api.conversations.mockResolvedValueOnce({
      items: [running],
      next_cursor: null,
      has_more: false,
    })
    const store = useButlerStore()
    await store.load('access')

    await expect(store.openSpecialist('CIVIL_SERVICE_EXAM', 'access')).resolves.toBe('WELCOME')
    expect(api.cancelRun).toHaveBeenCalledWith('run-current', 'access')
    expect(store.stagedSpecialistCode).toBe('CIVIL_SERVICE_EXAM')
  })

  it('requires confirmation before cancelling an executing scene switch', async () => {
    const running = {
      ...conversations.items[0],
      active_run: { id: 'run-current', status: 'RUNNING' },
    }
    api.conversations.mockResolvedValueOnce({
      items: [running],
      next_cursor: null,
      has_more: false,
    })
    const store = useButlerStore()
    await store.load('access')

    await expect(
      store.switchAssistantScene(
        { kind: 'SPECIALIST', specialistCode: 'CIVIL_SERVICE_EXAM' },
        'access',
      ),
    ).resolves.toBe('CONFIRMATION_REQUIRED')
    expect(api.cancelRun).not.toHaveBeenCalled()
    expect(store.activeConversationId).toBe('conversation-1')

    await expect(
      store.switchAssistantScene(
        { kind: 'SPECIALIST', specialistCode: 'CIVIL_SERVICE_EXAM' },
        'access',
        { cancelExecuting: true },
      ),
    ).resolves.toBe('WELCOME')
    expect(api.cancelRun).toHaveBeenCalledWith('run-current', 'access')
    expect(store.activeConversationId).toBeNull()
    expect(store.stagedScene).toEqual({
      kind: 'SPECIALIST',
      specialistCode: 'CIVIL_SERVICE_EXAM',
    })
  })

  it('stages the general butler and forces its first message into a new scene', async () => {
    const specialist = {
      ...conversations.items[0],
      specialist: { code: 'CIVIL_SERVICE_EXAM', name: '考公', icon: '公' },
    }
    api.conversations.mockResolvedValueOnce({
      items: [specialist],
      next_cursor: null,
      has_more: false,
    })
    const store = useButlerStore()
    await store.load('access')

    await expect(store.switchAssistantScene({ kind: 'GENERAL' }, 'access')).resolves.toBe('WELCOME')
    expect(store.activeConversationId).toBeNull()
    expect(store.stagedScene).toEqual({ kind: 'GENERAL' })

    api.sendMessage.mockResolvedValueOnce({
      conversation_id: 'conversation-general',
      transition: { kind: 'CREATED', archived_conversation_id: 'conversation-1' },
      run: { id: 'run-general' },
      stream: { events_url: '/v1/events', ticket: 'ticket', last_sequence: 0 },
    })
    await store.sendMessage('聊聊日常安排', 'access')

    expect(api.sendMessage).toHaveBeenLastCalledWith(
      expect.objectContaining({
        specialist_code: null,
        target_conversation_id: null,
        context_policy: 'ARCHIVE_AND_START',
      }),
      'access',
    )
    expect(store.stagedScene).toBeNull()
  })

  it('resumes the latest suspended general butler conversation', async () => {
    const specialist = {
      ...conversations.items[0],
      specialist: { code: 'CIVIL_SERVICE_EXAM', name: '考公', icon: '公' },
    }
    const suspendedGeneral = {
      ...conversations.items[0],
      id: 'conversation-general-waiting',
      status: 'ARCHIVED',
      active_run: { id: 'run-general-waiting', status: 'AWAITING_APPROVAL' },
    }
    api.conversations.mockResolvedValueOnce({
      items: [specialist, suspendedGeneral],
      next_cursor: null,
      has_more: false,
    })
    const store = useButlerStore()
    await store.load('access')

    await expect(store.switchAssistantScene({ kind: 'GENERAL' }, 'access')).resolves.toBe(
      'RESUMABLE',
    )
    expect(store.activeConversationId).toBe('conversation-general-waiting')
    expect(store.stagedScene).toBeNull()
  })

  it('recognizes an already active specialist and exposes all workflow status labels', async () => {
    const statusCases = [
      ['AWAITING_APPROVAL', '待确认'],
      ['FAILED_RETRYABLE', '待重试'],
      ['QUEUED', '处理中'],
    ] as const
    api.conversations.mockResolvedValueOnce({
      items: statusCases.map(([status], index) => ({
        ...conversations.items[0],
        id: `conversation-${index}`,
        status: index === 0 ? 'CURRENT' : 'ARCHIVED',
        specialist: { code: 'CIVIL_SERVICE_EXAM', name: '考公', icon: '公' },
        active_run: { id: `run-${index}`, status },
      })),
      next_cursor: null,
      has_more: false,
    })
    const store = useButlerStore()
    await store.load('access')

    expect(store.conversations.map((item) => item.statusLabel)).toEqual(
      statusCases.map(([, label]) => label),
    )
    await expect(store.openSpecialist('CIVIL_SERVICE_EXAM', 'access')).resolves.toBe('CURRENT')
  })

  it('approves plans, completes tasks, and resets state', async () => {
    const store = useButlerStore()
    await store.load('access')
    const item = store.chatItems.find((entry) => entry.kind === 'plan')
    if (!item || item.kind !== 'plan') throw new Error('missing plan card')
    await store.approvePlan(item, 'APPROVE', 'access')
    expect(item.status).toBe('approved')
    await store.completeTask('task-1', 'access')
    expect(store.tasks[0]?.status).toBe('DONE')
    store.reset()
    expect(store.chatItems).toEqual([])
  })

  it('refreshes the card projection after an approval version conflict', async () => {
    const store = useButlerStore()
    await store.load('access')
    const item = store.chatItems.find((entry) => entry.kind === 'plan')
    if (!item || item.kind !== 'plan') throw new Error('missing plan card')
    const callsBeforeApproval = api.messages.mock.calls.length
    api.approve.mockRejectedValueOnce(
      new ApiError(
        409,
        {
          error: {
            code: 'APPROVAL_VERSION_CONFLICT',
            message: '审批版本已更新，请刷新后重试',
          },
        },
        'APPROVAL_VERSION_CONFLICT',
      ),
    )
    await expect(store.approvePlan(item, 'APPROVE', 'access')).rejects.toMatchObject({
      code: 'APPROVAL_VERSION_CONFLICT',
    })
    expect(api.messages.mock.calls.length).toBe(callsBeforeApproval + 1)
  })

  it('surfaces load failures and reloads after stale task resources', async () => {
    const store = useButlerStore()
    api.dashboard.mockRejectedValueOnce(new Error('offline'))
    await expect(store.load('access')).rejects.toThrow('offline')
    expect(store.error).toBe('offline')

    await store.load('access')
    api.executeTask.mockRejectedValueOnce(new ApiError(404, {}, 'RESOURCE_NOT_FOUND'))
    await expect(store.completeTask('task-1', 'access')).rejects.toBeInstanceOf(ApiError)
    expect(api.dashboard).toHaveBeenCalled()
  })

  it('rejects malformed run responses', async () => {
    const store = useButlerStore()
    await store.load('access')
    api.sendMessage.mockResolvedValueOnce({})
    await expect(store.sendMessage('开始', 'access')).rejects.toThrow('无效的运行响应')
  })
})
