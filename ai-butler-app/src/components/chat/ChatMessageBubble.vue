<script setup lang="ts">
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import type { ChatItem } from '@/types/view-models'

defineProps<{ item: Extract<ChatItem, { kind: 'message' }> }>()
</script>

<template>
  <view class="message-row" :class="{ user: item.role === 'user' }">
    <view v-if="item.role === 'assistant'" class="bubble-avatar">✦</view>
    <view class="bubble" :class="item.role">
      <SafeMarkdown v-if="item.role === 'assistant'" :content="item.content" />
      <text v-else>{{ item.content }}</text>
    </view>
  </view>
</template>

<style scoped>
.message-row {
  display: flex;
  align-items: flex-start;
  gap: 14rpx;
}
.message-row.user {
  justify-content: flex-end;
}
.bubble-avatar {
  display: flex;
  flex: 0 0 auto;
  width: 58rpx;
  height: 58rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 24rpx;
  background: linear-gradient(145deg, #6d5be5, #a095f8);
  border-radius: 20rpx;
  box-shadow: 0 8rpx 24rpx rgba(91, 72, 205, 0.2);
}
.bubble {
  max-width: 78%;
  padding: 22rpx 25rpx;
  color: #312e43;
  font-size: 23rpx;
  line-height: 1.65;
  background: rgba(255, 255, 255, 0.9);
  border: 1px solid rgba(93, 78, 160, 0.1);
  border-radius: 9rpx 28rpx 28rpx 28rpx;
  box-shadow: 0 10rpx 28rpx rgba(52, 42, 96, 0.05);
}
.bubble.user {
  color: #fff;
  background: linear-gradient(135deg, #6454e8, #7968f3);
  border: 0;
  border-radius: 28rpx 9rpx 28rpx 28rpx;
}
</style>
