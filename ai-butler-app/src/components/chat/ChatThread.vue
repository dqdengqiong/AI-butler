<script setup lang="ts">
import ChatMessageBubble from '@/components/chat/ChatMessageBubble.vue'
import ChatPlanPreviewCard from '@/components/chat/ChatPlanPreviewCard.vue'
import ChatSourceCard from '@/components/chat/ChatSourceCard.vue'
import ChatStatusCard from '@/components/chat/ChatStatusCard.vue'
import type { ChatItem } from '@/types/view-models'

type PlanItem = Extract<ChatItem, { kind: 'planPreview' }>
type StatusItem = Extract<ChatItem, { kind: 'status' }>
defineProps<{ items: ChatItem[]; fresh: boolean }>()
defineEmits<{
  confirmPlan: [item: PlanItem]
  editPlan: [item: PlanItem]
  openSource: [citationId: string]
  retryRun: [item: StatusItem]
}>()
</script>

<template>
  <view class="chat-thread" :class="{ fresh }">
    <template v-for="item in items" :key="item.key">
      <ChatMessageBubble
        v-if="item.kind === 'message' && (!fresh || item.role === 'user')"
        :item="item"
      />
      <ChatPlanPreviewCard
        v-else-if="item.kind === 'planPreview'"
        :item="item"
        @confirm="(plan) => $emit('confirmPlan', plan)"
        @edit="(plan) => $emit('editPlan', plan)"
      />
      <ChatSourceCard
        v-else-if="item.kind === 'source'"
        :item="item"
        @open="(citationId) => $emit('openSource', citationId)"
      />
      <ChatStatusCard
        v-else-if="item.kind === 'status'"
        :item="item"
        @retry="(status) => $emit('retryRun', status)"
      />
    </template>
  </view>
</template>

<style scoped>
.chat-thread {
  display: flex;
  flex-direction: column;
  gap: 24rpx;
  padding: 0 2rpx 50rpx;
}
.chat-thread.fresh {
  display: none;
}
</style>
