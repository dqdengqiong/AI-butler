import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { parse } from '@vue/compiler-sfc'
import { describe, expect, it } from 'vitest'

const source = readFileSync(
  fileURLToPath(new URL('../src/components/ConversationDrawer.vue', import.meta.url)),
  'utf8',
)
const template = parse(source).descriptor.template?.content ?? ''

describe('conversation history deletion', () => {
  it('only renders the delete action for archived conversations', () => {
    const deleteAction = template.slice(
      template.indexOf('<button\n              v-if="conversation.archived"'),
      template.indexOf('</button>', template.indexOf('class="conversation-delete"')),
    )

    expect(deleteAction).toContain('class="conversation-delete"')
    expect(deleteAction).toContain("emit('delete', conversation.key)")
    expect(deleteAction).toContain('v-if="conversation.archived"')
  })

  it('has no manual create action and explains automatic organization', () => {
    expect(template).not.toContain('开启新对话')
    expect(template).toContain('系统会按话题自动整理')
    expect(template).toContain('空白页不计入历史')
    expect(template).toContain('conversation.statusLabel')
  })
})
