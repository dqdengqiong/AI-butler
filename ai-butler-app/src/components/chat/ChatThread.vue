<script setup lang="ts">
import ChatMessageBubble from '@/components/chat/ChatMessageBubble.vue'
import ChatPlanCard from '@/components/chat/ChatPlanCard.vue'
import ChatSelectionCard from '@/components/chat/ChatSelectionCard.vue'
import ChatSourceCard from '@/components/chat/ChatSourceCard.vue'
import ChatStatusCard from '@/components/chat/ChatStatusCard.vue'
import type { ChatItem } from '@/types/view-models'

type PlanItem = Extract<ChatItem, { kind: 'plan' }>
defineProps<{ items: ChatItem[]; fresh: boolean }>()
defineEmits<{
  selectOption: [itemKey: string, optionIndex: number]
  submitSelection: [itemKey: string]
  approvePlan: [item: PlanItem]
  editPlan: [item: PlanItem]
  rejectPlan: [item: PlanItem]
  openSource: [citationId: string]
}>()
</script>

<template>
  <view class="chat-thread" :class="{ fresh }">
    <template v-for="item in items" :key="item.key">
      <ChatMessageBubble
        v-if="item.kind === 'message' && (!fresh || item.role === 'user')"
        :item="item"
      />
      <ChatSelectionCard
        v-else-if="item.kind === 'selection'"
        :item="item"
        @select-option="(key, index) => $emit('selectOption', key, index)"
        @submit="(key) => $emit('submitSelection', key)"
      />
      <ChatPlanCard
        v-else-if="item.kind === 'plan'"
        :item="item"
        @approve="(plan) => $emit('approvePlan', plan)"
        @edit="(plan) => $emit('editPlan', plan)"
        @reject="(plan) => $emit('rejectPlan', plan)"
      />
      <ChatSourceCard
        v-else-if="item.kind === 'source'"
        :item="item"
        @open="(citationId) => $emit('openSource', citationId)"
      />
      <ChatStatusCard v-else-if="item.kind === 'status'" :item="item" />
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
