<script setup lang="ts">
import MarkdownIt from 'markdown-it'
import { computed } from 'vue'

const props = defineProps<{ content: string }>()

const renderer = new MarkdownIt({ html: false, linkify: false, breaks: true })
const defaultLinkOpen = renderer.renderer.rules.link_open
renderer.renderer.rules.link_open = (tokens, index, options, env, self) => {
  const token = tokens[index]
  if (!token) return ''
  const hrefIndex = token.attrIndex('href')
  const href = hrefIndex >= 0 ? token.attrs?.[hrefIndex]?.[1] : ''
  if (!href || !/^https:\/\//i.test(href)) {
    token.attrSet('href', '#')
  }
  token.attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpen
    ? defaultLinkOpen(tokens, index, options, env, self)
    : self.renderToken(tokens, index, options)
}

/** raw HTML 被 markdown-it 禁用，链接额外限制为 HTTPS。 */
const rendered = computed(() => renderer.render(props.content))
</script>

<template>
  <rich-text :nodes="rendered" />
</template>
