import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { parse } from '@vue/compiler-sfc'
import { describe, expect, it } from 'vitest'

const chatSource = readFileSync(
  fileURLToPath(new URL('../src/components/ChatView.vue', import.meta.url)),
  'utf8',
)
const welcomeSource = readFileSync(
  fileURLToPath(new URL('../src/components/chat/ChatWelcome.vue', import.meta.url)),
  'utf8',
)
const composerSource = readFileSync(
  fileURLToPath(new URL('../src/components/chat/ChatComposer.vue', import.meta.url)),
  'utf8',
)
const statusSource = readFileSync(
  fileURLToPath(new URL('../src/components/chat/ChatStatusCard.vue', import.meta.url)),
  'utf8',
)
const planSource = readFileSync(
  fileURLToPath(new URL('../src/components/chat/ChatPlanPreviewCard.vue', import.meta.url)),
  'utf8',
)
const chatTemplate = parse(chatSource).descriptor.template?.content ?? ''
const welcomeTemplate = parse(welcomeSource).descriptor.template?.content ?? ''
const composerTemplate = parse(composerSource).descriptor.template?.content ?? ''
const statusTemplate = parse(statusSource).descriptor.template?.content ?? ''
const planTemplate = parse(planSource).descriptor.template?.content ?? ''

describe('chat entry layout', () => {
  it('only shows agent discovery on the general empty welcome state', () => {
    expect(chatTemplate).toContain('v-if="isFreshConversation"')
    expect(chatTemplate).toContain('<ChatWelcome')
    expect(welcomeTemplate).toContain('v-if="!activeAgent"')
    expect(welcomeTemplate).toContain("$emit('selectAgent', agent.code)")
    expect(welcomeTemplate).toContain('class="agent-shortcut"')
  })

  it('keeps quick prompts above the composer outside the fresh-only branch', () => {
    expect(chatTemplate).toContain('<ChatComposer')
    expect(composerTemplate).toContain('class="quick-prompts"')
    expect(composerTemplate).toContain('@click="choosePrompt(prompt)"')
    expect(composerSource).toContain("prompt.behavior === 'FILL_COMPOSER'")
    expect(composerSource).toContain("emit('send', prompt.prompt)")
    expect(composerTemplate.indexOf('class="quick-prompts"')).toBeLessThan(
      composerTemplate.indexOf('class="composer-shell"'),
    )
    expect(composerTemplate).not.toContain('class="agent-shortcut"')
  })

  it('offers retry only for retryable terminal run errors', () => {
    expect(statusTemplate).toContain("item.state === 'error' && item.retryable !== false")
    expect(statusTemplate).toContain("$emit('retry', item)")
    expect(statusTemplate).toContain('重新生成')
  })

  it('shows the confirmed period and weekly investment on plan cards', () => {
    expect(planTemplate).toContain('{{ item.startDate }} 至 {{ item.endDate }}')
    expect(planTemplate).toContain('item.weeklyMinutes')
    expect(planTemplate).toContain('item.dailyAvailability.length')
    expect(planTemplate).toContain('未来 7 天可投入时间')
    expect(planTemplate).toContain('durationLabel(day.availableMinutes)')
    expect(planTemplate).toContain("item.status === 'READY'")
  })
})
