<script setup lang="ts">
import AppSheet from '@/components/AppSheet.vue'

defineProps<{
  open: boolean
  title: string
  excerpt: string
  organization: string
  typeLabel: string
  publishedAt: string
  retrievedAt: string
}>()
defineEmits<{ close: []; openOriginal: [] }>()
</script>

<template>
  <AppSheet :open="open" eyebrow="引用来源" :title="title" @close="$emit('close')">
    <view class="source-meta"
      ><text class="official-badge">{{ typeLabel }}</text
      ><text>{{ organization }}</text></view
    >
    <view class="source-detail"
      ><text class="source-heading">证据片段</text
      ><text class="source-copy">{{ excerpt }}</text></view
    >
    <view class="source-detail">
      <text class="source-heading">来源信息</text>
      <view class="audit-row"
        ><text>来源类型</text><text>{{ typeLabel }}</text></view
      >
      <view class="audit-row"
        ><text>发布时间</text><text>{{ publishedAt }}</text></view
      >
      <view class="audit-row"
        ><text>检索时间</text><text>{{ retrievedAt }}</text></view
      >
    </view>
    <button class="sheet-primary" @click="$emit('openOriginal')">查看资料原文</button>
  </AppSheet>
</template>

<style scoped>
.source-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12rpx;
  margin: 28rpx 0 20rpx;
  color: #737c90;
  font-size: 19rpx;
}
.official-badge {
  padding: 8rpx 14rpx;
  color: #168764;
  background: #e9f8f2;
  border-radius: 999rpx;
}
.source-detail {
  margin-bottom: 16rpx;
  padding: 23rpx;
  background: #f6f7fa;
  border-radius: 24rpx;
}
.source-heading,
.source-copy {
  display: block;
}
.source-heading {
  font-size: 22rpx;
  font-weight: 720;
}
.source-copy {
  margin-top: 13rpx;
  color: #596277;
  font-size: 20rpx;
  line-height: 1.65;
}
.audit-row {
  display: flex;
  justify-content: space-between;
  padding: 16rpx 0;
  font-size: 20rpx;
  border-bottom: 1px solid #e3e6ec;
}
.audit-row:last-child {
  border-bottom: 0;
}
.audit-row > text:last-child {
  color: #168764;
  font-weight: 700;
}
.sheet-primary {
  min-height: 82rpx;
  color: #fff;
  font-size: 23rpx;
  font-weight: 720;
  line-height: 82rpx;
  background: #596bff;
  border: 0;
  border-radius: 24rpx;
}
button::after {
  border: 0;
}
</style>
