import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { parse } from '@vue/compiler-sfc'
import { describe, expect, it } from 'vitest'

const source = readFileSync(
  fileURLToPath(new URL('../src/pages/index/index.vue', import.meta.url)),
  'utf8',
)
const parsed = parse(source).descriptor
const template = parsed.template?.content ?? ''
const styles = parsed.styles.map((style) => style.content).join('\n')

describe('persistent assistant switcher', () => {
  it('uses the sticky chat title as the accessible switch trigger', () => {
    expect(styles).toContain('.topbar {')
    expect(styles).toContain('position: sticky')
    expect(template).toContain('class="topbar-title assistant-switch-trigger"')
    expect(template).toContain('aria-label="切换 AI 管家或专业助理"')
    expect(template).toContain(':aria-expanded="activeSheet === \'assistants\'"')
    expect(template).toContain('@click="activeSheet = \'assistants\'"')
  })

  it('lists the general butler, specialists, current state and unavailable state', () => {
    const picker = template.slice(
      template.indexOf(':open="activeSheet === \'assistants\'"'),
      template.indexOf(':open="activeSheet === \'attachments\'"'),
    )

    expect(picker).toContain('title="切换助理"')
    expect(picker).toContain('AI 管家')
    expect(picker).toContain('v-for="agent in agentShortcuts"')
    expect(picker).toContain('isAssistantCurrent(agent.code)')
    expect(picker).toContain("agent.availability === 'COMING_SOON'")
    expect(picker).toContain('即将开放')
    expect(picker).toContain('assistantStatus(agent.code)')
  })
})
