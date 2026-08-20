import { describe, expect, it } from 'vitest'

import {
  asArray,
  asObject,
  mapAgentDefinition,
  mapConversation,
  mapMessage,
  mapPlan,
  mapTask,
  numberValue,
  runErrorPresentation,
  stringValue,
} from '@/stores/butler-projections'

describe('stateless chat projections', () => {
  it('ignores removed client action cards', () => {
    const items = mapMessage({
      id: 'message-1',
      role: 'ASSISTANT',
      content: '请填写表单',
      cards: {
        cards: [
          {
            schema_version: '1.0',
            card_id: 'action-1',
            card_type: 'ClientActionCard',
            payload: {
              action: 'OPEN_PLAN_FORM',
              operation: 'CREATE',
              objective_prefill: '准备省考',
            },
            actions: [],
          },
        ],
      },
    })
    expect(items.map((item) => item.kind)).toEqual(['message'])
  })

  it('maps a read-only preview and binds it to the assistant message', () => {
    const items = mapMessage({
      id: 'preview-message',
      role: 'ASSISTANT',
      content: '预览已生成',
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
              daily_availability: [
                {
                  date: '2026-08-18',
                  day_of_week: 2,
                  available_minutes: 60,
                  source: 'EXPLICIT_RULE',
                },
                {
                  date: '2026-08-19',
                  day_of_week: 3,
                  available_minutes: 0,
                  source: 'EXCLUDED_DATE',
                },
                { date: '', day_of_week: 8, available_minutes: -1, source: 'INVALID' },
              ],
              warnings: [],
            },
            actions: [],
          },
        ],
      },
    })
    expect(items[1]).toMatchObject({
      kind: 'planPreview',
      messageId: 'preview-message',
      status: 'READY',
      weeklyMinutes: 255,
      dailyAvailability: [
        {
          date: '2026-08-18',
          dayOfWeek: 2,
          availableMinutes: 60,
          source: 'EXPLICIT_RULE',
        },
        {
          date: '2026-08-19',
          dayOfWeek: 3,
          availableMinutes: 0,
          source: 'EXCLUDED_DATE',
        },
      ],
    })
  })

  it('has no waiting status projection', () => {
    const conversation = mapConversation({
      id: 'conversation-1',
      title: '对话',
      status: 'CURRENT',
      specialist: null,
      last_message: null,
      last_message_at: null,
      active_run: { id: 'run-1', status: 'RUNNING' },
      created_at: '2026-08-18T00:00:00Z',
      updated_at: '2026-08-18T00:00:00Z',
    })
    expect(conversation.statusLabel).toBe('处理中')
  })

  it('covers safe source, status and unknown card degradation', () => {
    const items = mapMessage({
      id: 'cards',
      role: 'SYSTEM_EVENT',
      cards: {
        cards: [
          {
            schema_version: '1.0',
            card_id: 'source',
            card_type: 'SourceCard',
            payload: {
              sources: [
                {
                  citation_id: 'citation',
                  index: 1,
                  source_type: 'INVALID',
                  source_level: 'INVALID',
                  published_at: 3,
                },
              ],
            },
          },
          {
            schema_version: '2.0',
            card_id: 'future-source',
            card_type: 'SourceCard',
            payload: { sources: [] },
          },
          { card_id: 'status', card_type: 'StatusCard', payload: {} },
          { card_id: 'unknown', card_type: 'UnknownCard', payload: {} },
        ],
      },
    })
    expect(items.map((item) => item.kind)).toEqual(['message', 'source', 'source', 'status'])
    expect(items[1]).toMatchObject({
      interactive: true,
      sources: [{ sourceType: 'KNOWLEDGE', sourceLevel: 'GENERAL', publishedAt: null }],
    })
    expect(items[2]).toMatchObject({ interactive: false, sources: [] })
  })

  it('covers fallback values and utility projections', () => {
    expect(mapPlan({ status: 'CANCELLED' })).toMatchObject({
      title: '公务员备考',
      statusLabel: '已结束',
      progressLabel: '0 / 0 项',
    })
    expect(mapTask({ status: 'DONE' })).toMatchObject({
      done: true,
      planTitle: '公务员备考',
    })
    expect(
      mapAgentDefinition({
        code: 'CIVIL',
        name: '考公',
        icon: '公',
        description: '说明',
        availability: 'AVAILABLE',
        welcome_message: '欢迎',
        starter_prompts: [],
      }),
    ).toMatchObject({ code: 'CIVIL', welcomeMessage: '欢迎' })
    expect(asObject(null)).toBeNull()
    expect(asArray([{}, null, 1])).toEqual([{}])
    expect(stringValue({ value: 1 }, 'value', 'fallback')).toBe('fallback')
    expect(numberValue({ value: '1' }, 'value', 2)).toBe(2)
  })

  it('covers terminal error and conversation time branches', () => {
    expect(runErrorPresentation('PLANNER_MODEL_UNAVAILABLE', true).title).toBe('计划生成超时')
    expect(runErrorPresentation('PLAN_HASH_INVALID', false).title).toBe('计划草稿校验失败')
    expect(runErrorPresentation('OTHER', false).title).toBe('回答生成失败')
    const base = {
      id: 'conversation',
      title: '对话',
      status: 'ARCHIVED' as const,
      specialist: { code: 'CIVIL', name: '考公', icon: '公' },
      last_message: { content: '最后消息', created_at: '2026-08-18T00:00:00Z' },
      active_run: { id: 'failed', status: 'FAILED_RETRYABLE' },
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-01T00:00:00Z',
    }
    expect(mapConversation({ ...base, last_message_at: new Date().toISOString() })).toMatchObject({
      section: 'today',
      archived: true,
      agentCode: 'CIVIL',
      statusLabel: '待重试',
    })
    expect(
      mapConversation({
        ...base,
        last_message_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
      }).section,
    ).toBe('week')
    expect(
      mapConversation({
        ...base,
        active_run: null,
        last_message_at: new Date(Date.now() - 10 * 86_400_000).toISOString(),
      }),
    ).toMatchObject({ section: 'earlier', statusLabel: '已完成' })
  })
})
