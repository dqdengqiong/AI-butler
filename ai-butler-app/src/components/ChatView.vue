<script setup lang="ts">
import { computed } from 'vue'

import ChatComposer from '@/components/chat/ChatComposer.vue'
import ChatThread from '@/components/chat/ChatThread.vue'
import ChatWelcome from '@/components/chat/ChatWelcome.vue'
import type {
  AgentShortcutCode,
  AgentShortcutViewModel,
  ChatItem,
  UploadedAttachment,
} from '@/types/view-models'

type PlanChatItem = Extract<ChatItem, { kind: 'plan' }>

const props = defineProps<{
  items: ChatItem[]
  attachments: UploadedAttachment[]
  userName: string
  agentShortcuts: AgentShortcutViewModel[]
  activeAgentCode?: AgentShortcutCode
}>()

const emit = defineEmits<{
  send: [content: string]
  openAttachments: []
  removeAttachment: [fileId: string]
  selectOption: [itemKey: string, optionIndex: number]
  submitSelection: [itemKey: string]
  approvePlan: [item: PlanChatItem]
  editPlan: [item: PlanChatItem]
  openSource: [citationId: string]
  selectAgent: [agentCode: AgentShortcutCode]
}>()

const isFreshConversation = computed(
  () => !props.items.some((item) => item.kind === 'message' && item.role === 'user'),
)
const activeAgent = computed(() =>
  props.agentShortcuts.find((agent) => agent.code === props.activeAgentCode),
)
</script>

<template>
  <view class="chat-view">
    <scroll-view class="chat-scroll" scroll-y :scroll-with-animation="true">
      <ChatWelcome
        v-if="isFreshConversation"
        :user-name="userName"
        :agents="agentShortcuts"
        :active-agent="activeAgent"
        :active-agent-code="activeAgentCode"
        @select-agent="(code) => emit('selectAgent', code)"
      />
      <view v-else class="conversation-date">今天</view>
      <ChatThread
        :items="items"
        :fresh="isFreshConversation"
        @select-option="(key, index) => emit('selectOption', key, index)"
        @submit-selection="(key) => emit('submitSelection', key)"
        @approve-plan="(item) => emit('approvePlan', item)"
        @edit-plan="(item) => emit('editPlan', item)"
        @open-source="(citationId) => emit('openSource', citationId)"
      />
    </scroll-view>
    <ChatComposer
      :items="items"
      :attachments="attachments"
      :active-agent="activeAgent"
      @send="(content) => emit('send', content)"
      @open-attachments="emit('openAttachments')"
      @remove-attachment="(fileId) => emit('removeAttachment', fileId)"
    />
  </view>
</template>

<style scoped>
.chat-view {
  position: relative;
  min-height: calc(100vh - 132rpx - env(safe-area-inset-top));
}
.chat-scroll {
  height: calc(100vh - 356rpx - env(safe-area-inset-top) - env(safe-area-inset-bottom));
}
.conversation-date {
  padding: 14rpx 0 28rpx;
  color: #aaa6b7;
  font-size: 18rpx;
  text-align: center;
}
</style>
