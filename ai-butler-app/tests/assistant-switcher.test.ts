import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { parse } from '@vue/compiler-sfc'
import { describe, expect, it } from 'vitest'

const pageSource = readFileSync(
  fileURLToPath(new URL('../src/pages/index/index.vue', import.meta.url)),
  'utf8',
)
const topbarSource = readFileSync(
  fileURLToPath(new URL('../src/components/page/MainTopbar.vue', import.meta.url)),
  'utf8',
)
const pickerSource = readFileSync(
  fileURLToPath(new URL('../src/components/page/AssistantPickerSheet.vue', import.meta.url)),
  'utf8',
)
const pageTemplate = parse(pageSource).descriptor.template?.content ?? ''
const topbar = parse(topbarSource).descriptor
const topbarTemplate = topbar.template?.content ?? ''
const topbarStyles = topbar.styles.map((style) => style.content).join('\n')
const pickerTemplate = parse(pickerSource).descriptor.template?.content ?? ''

describe('persistent assistant switcher', () => {
  it('uses the sticky chat title as the accessible switch trigger', () => {
    expect(pageTemplate).toContain('<MainTopbar')
    expect(pageTemplate).toContain(':assistants-open="activeSheet === \'assistants\'"')
    expect(pageTemplate).toContain('@open-assistants="activeSheet = \'assistants\'"')
    expect(topbarStyles).toContain('.topbar {')
    expect(topbarStyles).toContain('position: sticky')
    expect(topbarTemplate).toContain('class="topbar-title assistant-switch-trigger"')
    expect(topbarTemplate).toContain('aria-label="切换 AI 管家或专业助理"')
    expect(topbarTemplate).toContain(':aria-expanded="assistantsOpen"')
  })

  it('lists the general butler, specialists, current state and unavailable state', () => {
    expect(pageTemplate).toContain(':is-current="isAssistantCurrent"')
    expect(pageTemplate).toContain(':status-for="assistantStatus"')
    expect(pickerTemplate).toContain('title="切换助理"')
    expect(pickerTemplate).toContain('AI 管家')
    expect(pickerTemplate).toContain('v-for="agent in agents"')
    expect(pickerTemplate).toContain('isCurrent(agent.code)')
    expect(pickerTemplate).toContain("agent.availability === 'COMING_SOON'")
    expect(pickerTemplate).toContain('即将开放')
    expect(pickerTemplate).toContain('statusFor(agent.code)')
  })
})
