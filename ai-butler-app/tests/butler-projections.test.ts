import { describe, expect, it } from 'vitest'

import { mapMessage } from '@/stores/butler-projections'

function planMessage(card: Record<string, unknown>) {
  return mapMessage({ id: 'message', role: 'ASSISTANT', content: '草案', cards: { cards: [card] } })
}

describe('PlanCard projections', () => {
  it('maps a valid PlanCard 1.1 bundle with independent work items', () => {
    const items = planMessage({
      schema_version: '1.1',
      card_id: 'bundle-card',
      card_type: 'PlanCard',
      entity_refs: { approval_id: 'approval', approval_version: 1 },
      payload: {
        mode: 'BUNDLE_CREATE',
        title: '组合计划',
        total_weekly_minutes: 300,
        warnings: ['总负荷已按可用时间裁剪'],
        plans: [
          {
            work_item_id: 'work-1',
            title: '省考计划',
            objective_summary: '行测基础',
            weekly_minutes: 180,
          },
          {
            work_item_id: 'work-2',
            title: '申论计划',
            objective_summary: '申论训练',
            weekly_minutes: 120,
          },
        ],
      },
    })

    expect(items[1]).toMatchObject({
      kind: 'plan',
      schemaVersion: '1.1',
      mode: 'BUNDLE_CREATE',
      weeklyMinutes: 300,
      warnings: ['总负荷已按可用时间裁剪'],
      plans: [
        { key: 'work-1', weeklyMinutes: 180 },
        { key: 'work-2', weeklyMinutes: 120 },
      ],
    })
  })

  it('downgrades unknown and cardinality-invalid plan cards to read-only status', () => {
    const future = planMessage({
      schema_version: '2.0',
      card_id: 'future',
      card_type: 'PlanCard',
    })
    const invalid = planMessage({
      schema_version: '1.1',
      card_id: 'invalid',
      card_type: 'PlanCard',
      entity_refs: { approval_id: 'approval', approval_version: 1 },
      payload: { mode: 'BUNDLE_CREATE', plans: [{ title: 'only one' }] },
    })

    expect(future[1]).toMatchObject({ kind: 'status', title: '当前计划卡版本暂不支持' })
    expect(invalid[1]).toMatchObject({ kind: 'status', title: '计划草案结构无效' })
  })

  it('maps a rejected historical 1.0 card without making it pending', () => {
    const items = planMessage({
      schema_version: '1.0',
      card_id: 'legacy',
      card_type: 'PlanCard',
      entity_refs: {
        approval_id: 'approval',
        approval_version: 3,
        approval_status: 'REJECTED',
      },
      payload: { title: '旧计划', objective_summary: '历史草案', weekly_minutes: 60 },
    })

    expect(items[1]).toMatchObject({
      kind: 'plan',
      schemaVersion: '1.0',
      status: 'rejected',
      plans: [{ weeklyMinutes: 60 }],
    })
  })
})
