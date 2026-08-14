import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  dashboard: vi.fn(),
  agentDefinitions: vi.fn(),
  conversations: vi.fn(),
  createConversation: vi.fn(),
  messages: vi.fn(),
  sendMessage: vi.fn(),
  run: vi.fn(),
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
    api.createConversation.mockReset().mockResolvedValue(conversations.items[0])
    api.messages.mockReset().mockResolvedValue(messages)
    api.sendMessage.mockReset().mockResolvedValue({
      run: { id: 'run-1' },
      stream: { events_url: '/v1/events', ticket: 'ticket', last_sequence: 0 },
    })
    api.run.mockReset().mockResolvedValue({ status: 'SUCCEEDED' })
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
      'conversation-1',
      expect.objectContaining({
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
    await onEvent({ event: 'message.delta', payload: { delta: '增量' } })
    expect(
      store.chatItems.find((item) => item.kind === 'message' && item.role === 'assistant'),
    ).toBeTruthy()
    await onEvent({ event: 'message.reset', payload: {} })
    await onEvent({ event: 'message.completed', payload: {} })
    await onEvent({ event: 'run.completed', payload: {} })
    expect(store.activeRunId).toBeNull()
    expect(api.dashboard).toHaveBeenCalledTimes(2)
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
