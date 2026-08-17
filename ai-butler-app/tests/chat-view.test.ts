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
const chatTemplate = parse(chatSource).descriptor.template?.content ?? ''
const welcomeTemplate = parse(welcomeSource).descriptor.template?.content ?? ''
const composerTemplate = parse(composerSource).descriptor.template?.content ?? ''

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
    expect(composerTemplate).toContain('@click="draft = prompt.prompt"')
    expect(composerTemplate.indexOf('class="quick-prompts"')).toBeLessThan(
      composerTemplate.indexOf('class="composer-shell"'),
    )
    expect(composerTemplate).not.toContain('class="agent-shortcut"')
  })
})
