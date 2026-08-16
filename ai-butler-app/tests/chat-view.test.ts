import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { parse } from '@vue/compiler-sfc'
import { describe, expect, it } from 'vitest'

const source = readFileSync(
  fileURLToPath(new URL('../src/components/ChatView.vue', import.meta.url)),
  'utf8',
)
const template = parse(source).descriptor.template?.content ?? ''

describe('chat entry layout', () => {
  it('only shows agent discovery on the general empty welcome state', () => {
    const shortcut = template.slice(
      template.indexOf('<scroll-view\n        v-if="isFreshConversation && !activeAgent"'),
      template.indexOf('</scroll-view>', template.indexOf('class="agent-shortcut-scroll"')),
    )

    expect(shortcut).toContain('v-if="isFreshConversation && !activeAgent"')
    expect(shortcut).toContain("emit('selectAgent', agent.code)")
    expect(shortcut).toContain('class="agent-shortcut"')
  })

  it('keeps quick prompts above the composer outside the fresh-only branch', () => {
    const composer = template.slice(template.indexOf('<view class="composer-area">'))

    expect(composer).toContain('class="quick-prompts"')
    expect(composer).toContain('@click="usePrompt(prompt.prompt)"')
    expect(composer.indexOf('class="quick-prompts"')).toBeLessThan(
      composer.indexOf('class="composer-shell"'),
    )
    expect(composer).not.toContain('class="agent-shortcut"')
  })
})
