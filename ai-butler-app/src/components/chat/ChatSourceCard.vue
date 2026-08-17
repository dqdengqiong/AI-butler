<script setup lang="ts">
import type { ChatItem } from '@/types/view-models'

defineProps<{ item: Extract<ChatItem, { kind: 'source' }> }>()
defineEmits<{ open: [citationId: string] }>()
</script>

<template>
  <view class="message-card">
    <text class="card-label">引用 {{ item.sources.length }} 篇资料</text>
    <text class="card-title">{{ item.title }}</text>
    <text v-if="!item.interactive" class="card-description"
      >该卡片仅展示安全文本，不能打开来源。</text
    >
    <view v-else class="source-list">
      <button
        v-for="source in item.sources"
        :key="source.citationId"
        class="source-row"
        :disabled="!source.citationId"
        @click="$emit('open', source.citationId)"
      >
        <text class="source-index">{{ source.index }}</text>
        <view>
          <text class="source-title">{{ source.title }}</text>
          <text class="source-meta"
            >{{
              source.sourceType === 'PRIVATE_FILE'
                ? '我的资料'
                : source.sourceLevel === 'OFFICIAL'
                  ? '官方来源'
                  : '网页'
            }}
            · {{ source.domain }}</text
          >
        </view>
        <text class="source-arrow">›</text>
      </button>
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
.card-label {
  display: block;
  margin-bottom: 14rpx;
  color: #6556e8;
  font-size: 18rpx;
  font-weight: 750;
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
.source-list {
  display: flex;
  flex-direction: column;
  gap: 12rpx;
  margin-top: 18rpx;
}
.source-row {
  display: grid;
  width: 100%;
  min-height: auto;
  grid-template-columns: 48rpx minmax(0, 1fr) auto;
  align-items: center;
  gap: 12rpx;
  margin: 0;
  padding: 17rpx;
  line-height: 1.3;
  text-align: left;
  background: #f6f5fb;
  border: 0;
  border-radius: 22rpx;
}
.source-index {
  width: 42rpx;
  height: 42rpx;
  color: #6556e8;
  font-size: 19rpx;
  font-weight: 750;
  line-height: 42rpx;
  text-align: center;
  background: #ebe8ff;
  border-radius: 14rpx;
}
.source-row > view {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4rpx;
}
.source-title {
  overflow: hidden;
  font-size: 19rpx;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-meta {
  color: #7d788f;
  font-size: 17rpx;
}
.source-arrow {
  color: #9792a6;
  font-size: 30rpx;
}
button::after {
  border: 0;
}
</style>
