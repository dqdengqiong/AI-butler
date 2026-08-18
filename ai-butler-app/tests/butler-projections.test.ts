import { describe, expect, it } from 'vitest'

import { mapMessage, runErrorPresentation } from '@/stores/butler-projections'

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
            start_date: '2026-08-18',
            end_date: '2026-10-12',
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
        {
          key: 'work-1',
          weeklyMinutes: 180,
          startDate: '2026-08-18',
          endDate: '2026-10-12',
        },
        { key: 'work-2', weeklyMinutes: 120 },
      ],
    })
  })

  it('uses plan-specific failure copy without implying that a plan was created', () => {
    expect(runErrorPresentation('PLANNER_MODEL_UNAVAILABLE', true)).toEqual({
      title: '计划生成超时',
      description: '计划生成超时，本次未创建计划，可以重试。',
    })
    expect(runErrorPresentation('PLANNER_MODEL_INVALID', false)).toEqual({
      title: '计划草稿校验失败',
      description: '计划草稿未通过校验，本次未创建计划。',
    })
  })

  it('projects period collection and final scope confirmation cards', () => {
    const period = planMessage({
      card_id: 'period',
      card_type: 'SelectionCard',
      payload: {
        phase: 'COLLECT_PLAN_PERIOD',
        question: '选择计划周期',
        input_mode: 'NATURAL_LANGUAGE',
        options: [{ id: 'period-4-weeks', label: '4 周' }],
      },
      actions: [{ action_id: 'submit-selection', label: '确认周期' }],
    })[1]
    const confirmation = planMessage({
      card_id: 'scope',
      card_type: 'SelectionCard',
      payload: {
        phase: 'CONFIRM_PLAN_SCOPE',
        question: '确认计划范围',
        input_mode: 'SINGLE_SELECT',
        options: [{ id: 'confirm-plan-scope', label: '确认并生成计划' }],
      },
      actions: [{ action_id: 'submit-selection', label: '提交' }],
    })[1]

    expect(period).toMatchObject({
      kind: 'selection',
      allowFreeText: true,
      options: ['4 周'],
    })
    expect(confirmation).toMatchObject({
      kind: 'selection',
      allowFreeText: false,
      options: ['确认并生成计划'],
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
