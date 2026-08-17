<script setup lang="ts">
import type { ChatItem } from '@/types/view-models'

defineProps<{ item: Extract<ChatItem, { kind: 'status' }> }>()
</script>

<template>
  <view class="message-card">
    <view class="status-heading">
      <view class="spinner" />
      <view
        ><text class="card-title">{{ item.title }}</text
        ><text class="card-description">{{ item.description }}</text></view
      >
    </view>
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
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
