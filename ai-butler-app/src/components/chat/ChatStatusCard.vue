<script setup lang="ts">
import type { ChatItem } from '@/types/view-models'

type StatusItem = Extract<ChatItem, { kind: 'status' }>

defineProps<{ item: StatusItem }>()
defineEmits<{ retry: [item: StatusItem] }>()
</script>

<template>
  <view class="message-card" :class="{ error: item.state === 'error' }">
    <view class="status-heading">
      <view v-if="item.state !== 'error'" class="spinner" />
      <view v-else class="status-icon">!</view>
      <view
        ><text class="card-title">{{ item.title }}</text
        ><text class="card-description">{{ item.description }}</text></view
      >
    </view>
    <button
      v-if="item.state === 'error' && item.retryable !== false && item.runId"
      class="retry-button"
      :disabled="item.retrying"
      @click="$emit('retry', item)"
    >
      {{ item.retrying ? '正在重试…' : '重新生成' }}
    </button>
  </view>
</template>

<style scoped>
.message-card {
  margin-left: 72rpx;
  padding: 26rpx;
  background: rgba(255, 255, 255, 0.94);
  border: 1px solid rgba(96, 78, 177, 0.12);
  border-radius: 30rpx;
  box-shadow: 0 14rpx 38rpx rgba(48, 39, 93, 0.07);
}
.status-heading {
  display: flex;
  gap: 18rpx;
}
.status-heading > view:last-child {
  display: flex;
  flex: 1;
  flex-direction: column;
}
.card-title {
  display: block;
  color: #2f2b40;
  font-size: 26rpx;
  font-weight: 730;
}
.card-description {
  display: block;
  margin-top: 10rpx;
  color: #7f7a91;
  font-size: 20rpx;
  line-height: 1.55;
}
.spinner {
  width: 40rpx;
  height: 40rpx;
  flex: 0 0 auto;
  border: 5rpx solid #ddd8ff;
  border-top-color: #6556e8;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}
.message-card.error {
  border-color: rgba(205, 74, 74, 0.2);
}
.status-icon {
  display: flex;
  width: 40rpx;
  height: 40rpx;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: #fff0f0;
  color: #c94c4c;
  font-size: 26rpx;
  font-weight: 750;
}
.retry-button {
  min-height: 72rpx;
  margin: 22rpx 0 0 58rpx;
  border: 1px solid #7565ee;
  border-radius: 20rpx;
  background: #f4f1ff;
  color: #6556e8;
  font-size: 22rpx;
  font-weight: 700;
  line-height: 70rpx;
}
.retry-button::after {
  border: 0;
}
.retry-button[disabled] {
  opacity: 0.55;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
