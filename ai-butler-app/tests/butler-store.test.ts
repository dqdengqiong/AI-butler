import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  dashboard: vi.fn(),
  agentDefinitions: vi.fn(),
  conversations: vi.fn(),
  deleteConversation: vi.fn(),
  messages: vi.fn(),
  sendMessage: vi.fn(),
  deletePlan: vi.fn(),
  confirmPlanPreview: vi.fn(),
  run: vi.fn(),
  cancelRun: vi.fn(),
  retryRun: vi.fn(),
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
      content: '计划预览已生成',
      cards: {
        cards: [
          {
            schema_version: '1.0',
            card_id: 'preview-card',
            card_type: 'PlanPreviewCard',
            payload: {
              status: 'READY',
              operation: 'CREATE',
              title: '省考计划',
              total_weekly_minutes: 255,
              available_weekly_minutes: 300,
              preview_hash: 'a'.repeat(64),
              plan: {
                objective_summary: '准备省考',
                start_date: '2026-08-18',
                end_date: '2026-09-15',
              },
              warnings: [],
            },
          },
          {
            schema_version: '1.0',
            card_id: 'source-card',
            card_type: 'SourceCard',
            payload: {
              title: '来源',
              sources: [
                {
                  citation_id: 'citation-1',
                  index: 1,
                  title: '公告',
                  domain: 'gov.example',
                  source_type: 'WEB',
                  source_level: 'OFFICIAL',
                  published_at: null,
                },
              ],
            },
          },
          {
            schema_version: '1.0',
            card_id: 'status-card',
            card_type: 'StatusCard',
            payload: { title: '处理中', description: '正在生成' },
          },
        ],
      },
    },
  ],
  next_cursor: null,
  has_more: false,
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
    api.deletePlan.mockReset().mockResolvedValue(undefined)
    api.confirmPlanPreview.mockReset().mockResolvedValue({ status: 'CONFIRMED' })
    api.run.mockReset().mockResolvedValue({ status: 'SUCCEEDED' })
    api.cancelRun.mockReset().mockResolvedValue({ status: 'CANCELLED' })
    api.retryRun.mockReset().mockResolvedValue({ status: 'QUEUED', attempt: 1 })
    api.streamTicket.mockReset().mockResolvedValue({
      events_url: '/v1/events-2',
      ticket: 'ticket-2',
      last_sequence: 3,
    })
    api.executeTask.mockReset().mockResolvedValue({})
  })

  it('loads dashboard, cards and conversations', async () => {
    const store = useButlerStore()
    await store.load('access')
    expect(store.plans[0]?.progress).toBe(50)
    expect(store.pendingTasks).toHaveLength(1)
    expect(store.chatItems.map((item) => item.kind)).toEqual([
      'message',
      'message',
      'planPreview',
      'source',
      'status',
    ])
    expect(store.agentShortcuts[0]?.code).toBe('CIVIL_SERVICE_EXAM')
  })

  it('sends attachments and reconciles all public stream events', async () => {
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('目标', 'access', ['file-1'])
    expect(api.sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        context_policy: 'AUTO',
        attachments: [{ file_id: 'file-1', position: 0 }],
      }),
      'access',
    )
    const onEvent = stream.options?.onEvent as (event: Record<string, unknown>) => Promise<void>
    await onEvent({ event: 'progress', runId: 'run-1', payload: { code: 'SEARCHING_WEB' } })
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
  })

  it('projects errors and retries the whole run', async () => {
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('问题', 'access')
    const onEvent = stream.options?.onEvent as (event: Record<string, unknown>) => Promise<void>
    await onEvent({
      event: 'error',
      runId: 'run-1',
      attempt: 0,
      payload: { code: 'RAG_MODEL_UNAVAILABLE', message: '暂时不可用', retryable: true },
    })
    expect(store.chatItems.at(-1)).toMatchObject({ state: 'error', retryable: true })
    await store.retryRun('run-1', 0, 'access')
    expect(api.retryRun).toHaveBeenCalledWith(
      'run-1',
      expect.objectContaining({ expected_attempt: 0 }),
      'access',
    )
  })

  it('reloads attempt data for a restored retryable run', async () => {
    api.conversations.mockResolvedValue({
      ...conversations,
      items: [
        { ...conversations.items[0], active_run: { id: 'failed', status: 'FAILED_RETRYABLE' } },
      ],
    })
    api.run.mockResolvedValue({
      status: 'FAILED_RETRYABLE',
      attempt: 2,
      error: { code: 'PLANNER_MODEL_UNAVAILABLE', retryable: true },
    })
    const store = useButlerStore()
    await store.load('access')
    expect(store.chatItems.at(-1)).toMatchObject({ runId: 'failed', attempt: 2 })
    await store.retryRun('failed', undefined, 'access')
    expect(api.retryRun).toHaveBeenCalledWith(
      'failed',
      expect.objectContaining({ expected_attempt: 2 }),
      'access',
    )
  })

  it('creates a plan request through ordinary messaging and confirms its preview', async () => {
    const store = useButlerStore()
    await store.load('access')
    await store.sendMessage('帮我制定省考计划', 'access')
    expect(api.sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({ content: '帮我制定省考计划' }),
      'access',
    )
    const item = store.chatItems.find((entry) => entry.kind === 'planPreview')
    if (!item || item.kind !== 'planPreview') throw new Error('missing preview')
    await store.confirmPlanPreview(item, 'access')
    expect(api.confirmPlanPreview).toHaveBeenCalledWith(
      'm-agent',
      expect.objectContaining({ expected_preview_hash: 'a'.repeat(64) }),
      'access',
    )
    expect(item.status).toBe('CONFIRMED')
  })

  it('deletes a plan and refreshes dashboard projections', async () => {
    const store = useButlerStore()
    await store.load('access')
    await store.deletePlan('p1', 'access')
    expect(api.deletePlan).toHaveBeenCalledWith('p1', 'access')
    expect(api.dashboard).toHaveBeenCalledTimes(2)
  })

  it('stages scenes and can cancel a running scene', async () => {
    api.conversations.mockResolvedValueOnce({
      ...conversations,
      items: [{ ...conversations.items[0], active_run: { id: 'running', status: 'RUNNING' } }],
    })
    const store = useButlerStore()
    await store.load('access')
    await expect(
      store.switchAssistantScene(
        { kind: 'SPECIALIST', specialistCode: 'CIVIL_SERVICE_EXAM' },
        'access',
      ),
    ).resolves.toBe('CONFIRMATION_REQUIRED')
    await expect(store.openSpecialist('CIVIL_SERVICE_EXAM', 'access')).resolves.toBe('WELCOME')
    expect(api.cancelRun).toHaveBeenCalledWith('running', 'access')
    expect(store.activeConversationId).toBeNull()
  })

  it('switches on the first specialist message and handles terminal compensation', async () => {
    const store = useButlerStore()
    await store.load('access')
    await expect(store.openSpecialist('CIVIL_SERVICE_EXAM', 'access')).resolves.toBe('WELCOME')
    api.sendMessage.mockResolvedValueOnce({
      conversation_id: 'conversation-2',
      transition: { kind: 'CREATED', archived_conversation_id: 'conversation-1' },
      run: { id: 'run-2' },
      stream: { events_url: '/events-2', ticket: 'ticket-2', last_sequence: 0 },
    })
    await store.sendMessage('开始', 'access')
    expect(api.sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({ specialist_code: 'CIVIL_SERVICE_EXAM' }),
      'access',
    )
    const onError = stream.options?.onError as () => Promise<void>
    await onError()
    expect(api.run).toHaveBeenCalled()
  })

  it('deletes history, completes tasks, handles stale data and resets', async () => {
    const current = conversations.items[0]
    const history = { ...current, id: 'history', status: 'ARCHIVED' }
    api.conversations.mockResolvedValueOnce({ ...conversations, items: [current, history] })
    const store = useButlerStore()
    await store.load('access')
    await store.loadConversation('history', 'access')
    api.conversations.mockResolvedValueOnce(conversations)
    await store.deleteConversation('history', 'access')
    expect(store.activeConversationId).toBe('conversation-1')
    await store.completeTask('task-1', 'access')
    expect(store.tasks[0]?.status).toBe('DONE')
    api.executeTask.mockRejectedValueOnce(new ApiError(404, {}, 'RESOURCE_NOT_FOUND'))
    await expect(store.completeTask('task-1', 'access')).rejects.toBeInstanceOf(ApiError)
    store.reset()
    expect(store.chatItems).toEqual([])
  })

  it('surfaces load failures and rejects malformed runs', async () => {
    const store = useButlerStore()
    api.dashboard.mockRejectedValueOnce(new Error('offline'))
    await expect(store.load('access')).rejects.toThrow('offline')
    expect(store.error).toBe('offline')
    await store.load('access')
    api.sendMessage.mockResolvedValueOnce({})
    await expect(store.sendMessage('开始', 'access')).rejects.toThrow('无效的运行响应')
  })
})
