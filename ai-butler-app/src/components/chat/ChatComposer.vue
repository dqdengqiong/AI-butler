<script setup lang="ts">
import { computed, ref } from 'vue'

import type { AgentShortcutViewModel, ChatItem, UploadedAttachment } from '@/types/view-models'

type SelectionItem = Extract<ChatItem, { kind: 'selection' }>
const props = defineProps<{
  items: ChatItem[]
  attachments: UploadedAttachment[]
  activeAgent?: AgentShortcutViewModel
}>()
const emit = defineEmits<{
  send: [content: string]
  openAttachments: []
  removeAttachment: [fileId: string]
}>()

const draft = ref('')
const isRecording = ref(false)
const canSend = computed(() => draft.value.trim().length > 0 || props.attachments.length > 0)
const activeNaturalLanguagePrompt = computed<SelectionItem | undefined>(() => {
  for (let index = props.items.length - 1; index >= 0; index -= 1) {
    const item = props.items[index]
    if (item?.kind === 'selection' && item.allowFreeText && !item.submitted) return item
  }
  return undefined
})
const placeholder = computed(() =>
  isRecording.value
    ? '正在聆听…'
    : activeNaturalLanguagePrompt.value?.inputPlaceholder || '发消息或按住说话…',
)
const quickPrompts = computed(() =>
  props.activeAgent
    ? props.activeAgent.starterPrompts.map((prompt) => ({
        icon: props.activeAgent?.icon ?? '✦',
        title: prompt.label,
        prompt: prompt.content,
      }))
    : [
        { icon: '◎', title: '规划今天', prompt: '根据我的计划，帮我安排今天最重要的三件事' },
        { icon: '✓', title: '复盘进度', prompt: '帮我复盘本周计划进度，并给出调整建议' },
        { icon: '◌', title: '查找资料', prompt: '帮我查找可靠资料，并标注信息来源' },
      ],
)

function submit(): void {
  if (!canSend.value) {
    isRecording.value = !isRecording.value
    return
  }
  emit('send', draft.value.trim())
  draft.value = ''
  isRecording.value = false
}
</script>

<template>
  <view class="composer-area">
    <view class="quick-prompts">
      <button v-for="prompt in quickPrompts" :key="prompt.title" @click="draft = prompt.prompt">
        <text class="prompt-icon">{{ prompt.icon }}</text
        ><text>{{ prompt.title }}</text>
      </button>
    </view>
    <view class="composer-shell">
      <button
        class="voice-button"
        :class="{ recording: isRecording }"
        aria-label="语音输入"
        @click="isRecording = !isRecording"
      >
        {{ isRecording ? '■' : '◖))' }}
      </button>
      <view class="input-wrap" :class="{ recording: isRecording }">
        <view v-if="attachments.length" class="attachment-list">
          <view v-for="attachment in attachments" :key="attachment.id" class="attachment-chip">
            <text>文</text><text class="attachment-name">{{ attachment.name }}</text>
            <button @click="$emit('removeAttachment', attachment.id)">×</button>
          </view>
        </view>
        <input v-model="draft" :placeholder="placeholder" confirm-type="send" @confirm="submit" />
      </view>
      <button class="attach-button" aria-label="添加资料" @click="$emit('openAttachments')">
        ＋
      </button>
      <button v-if="canSend" class="composer-action" aria-label="发送" @click="submit">↑</button>
    </view>
    <text class="ai-note">内容由 AI 生成，请核对重要信息</text>
  </view>
</template>

<style scoped>
.composer-area {
  position: fixed;
  z-index: 20;
  right: 0;
  bottom: 0;
  left: 0;
  box-sizing: border-box;
  padding: 10rpx 24rpx calc(13rpx + env(safe-area-inset-bottom));
  background: linear-gradient(180deg, rgba(243, 241, 255, 0), #f3f1ff 20%);
}
.quick-prompts {
  display: grid;
  width: 100%;
  grid-template-columns: repeat(3, 1fr);
  gap: 14rpx;
  margin-bottom: 12rpx;
}
.quick-prompts button {
  display: flex;
  min-height: 88rpx;
  align-items: center;
  justify-content: center;
  gap: 10rpx;
  margin: 0;
  padding: 15rpx 8rpx;
  color: #56516d;
  font-size: 19rpx;
  line-height: 1.2;
  background: rgba(255, 255, 255, 0.74);
  border: 1px solid rgba(119, 101, 239, 0.11);
  border-radius: 25rpx;
  box-shadow: 0 10rpx 28rpx rgba(66, 54, 112, 0.05);
  flex-direction: column;
}
.prompt-icon {
  color: #6c5ce7;
  font-size: 28rpx;
}
.composer-shell {
  display: flex;
  min-height: 92rpx;
  align-items: flex-end;
  gap: 8rpx;
  padding: 10rpx;
  background: rgba(255, 255, 255, 0.97);
  border: 1px solid rgba(88, 70, 163, 0.13);
  border-radius: 32rpx;
  box-shadow: 0 18rpx 50rpx rgba(55, 43, 105, 0.15);
}
.voice-button,
.attach-button,
.composer-action {
  flex: 0 0 auto;
  width: 70rpx;
  height: 70rpx;
  margin: 0;
  padding: 0;
  color: #4d485f;
  font-size: 24rpx;
  line-height: 70rpx;
  background: transparent;
  border: 0;
  border-radius: 22rpx;
}
.voice-button.recording {
  color: #fff;
  background: #e55763;
}
.attach-button {
  font-size: 38rpx;
}
.composer-action {
  color: #fff;
  font-size: 32rpx;
  background: #6556e8;
}
.input-wrap {
  display: flex;
  min-width: 0;
  min-height: 70rpx;
  flex: 1;
  justify-content: center;
  flex-direction: column;
}
.input-wrap input {
  width: 100%;
  height: 70rpx;
  color: #292638;
  font-size: 23rpx;
}
.attachment-list {
  display: flex;
  gap: 8rpx;
  padding: 5rpx 0;
  flex-wrap: wrap;
}
.attachment-chip {
  display: flex;
  max-width: 300rpx;
  align-items: center;
  gap: 7rpx;
  padding: 6rpx 10rpx;
  color: #5b4fc5;
  font-size: 17rpx;
  background: #efecff;
  border-radius: 12rpx;
}
.attachment-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attachment-chip button {
  width: 30rpx;
  height: 30rpx;
  margin: 0;
  padding: 0;
  color: #7b75a1;
  font-size: 22rpx;
  line-height: 28rpx;
  background: transparent;
  border: 0;
}
.ai-note {
  display: block;
  margin-top: 9rpx;
  color: #aaa6b6;
  font-size: 15rpx;
  text-align: center;
}
button::after {
  border: 0;
}
</style>
