<script setup lang="ts">
import type { ChatItem } from '@/types/view-models'

type PlanItem = Extract<ChatItem, { kind: 'planPreview' }>
defineProps<{ item: PlanItem }>()
defineEmits<{
  confirm: [item: PlanItem]
  edit: [item: PlanItem]
}>()
</script>

<template>
  <view class="message-card">
    <view class="plan-heading">
      <view>
        <text class="card-label">计划预览</text>
        <text class="card-title">{{ item.title }}</text>
      </view>
      <text class="preview-pill" :class="item.status">
        {{ item.status === 'CONFIRMED' ? '已确认' : item.status === 'READY' ? '待确认' : '已失效' }}
      </text>
    </view>
    <view class="plan-block">
      <view class="plan-block-head"
        ><text>{{ item.operation === 'ADJUST' ? '调整现有计划' : '创建新计划' }}</text
        ><text>每周 {{ Math.round(item.weeklyMinutes / 6) / 10 }} 小时</text></view
      >
      <text class="plan-period"> {{ item.startDate }} 至 {{ item.endDate }} </text>
      <text class="plan-note">{{ item.description }}</text>
    </view>
    <text v-for="warning in item.warnings" :key="warning" class="plan-warning">{{ warning }}</text>
    <view v-if="item.status === 'READY'" class="card-actions">
      <button class="small-button secondary" @click="$emit('edit', item)">编辑</button>
      <button
        class="small-button primary"
        :disabled="item.confirming"
        @click="$emit('confirm', item)"
      >
        {{ item.confirming ? '确认中…' : '确认计划' }}
      </button>
    </view>
    <text v-else-if="item.status === 'CONFIRMED'" class="plan-result">✓ 正式计划和任务已创建</text>
    <text v-else class="plan-result editing">此预览已失效，请重新生成</text>
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
.plan-heading,
.plan-heading > view {
  display: flex;
}
.plan-heading {
  align-items: flex-start;
  justify-content: space-between;
  gap: 14rpx;
}
.plan-heading > view {
  min-width: 0;
  flex: 1;
  flex-direction: column;
}
.preview-pill {
  flex: 0 0 auto;
  padding: 8rpx 13rpx;
  color: #98611f;
  font-size: 18rpx;
  background: #fff1d9;
  border-radius: 999rpx;
}
.preview-pill.CONFIRMED {
  color: #168764;
  background: #e8f7f1;
}
.plan-block {
  margin-top: 20rpx;
  padding: 20rpx;
  background: #f7f6fb;
  border-radius: 22rpx;
}
.plan-block-head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 15rpx;
  font-size: 21rpx;
  font-weight: 700;
}
.plan-note {
  display: block;
  padding-top: 15rpx;
  color: #747083;
  font-size: 19rpx;
  line-height: 1.55;
  border-top: 1px solid #e6e2ed;
}
.plan-period {
  display: block;
  margin-bottom: 12rpx;
  color: #6556e8;
  font-size: 19rpx;
  font-weight: 650;
}
.card-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12rpx;
  margin-top: 22rpx;
}
.small-button {
  min-height: 68rpx;
  margin: 0;
  padding: 0 22rpx;
  font-size: 20rpx;
  font-weight: 700;
  line-height: 68rpx;
  border: 0;
  border-radius: 19rpx;
}
.small-button.primary {
  color: #fff;
  background: #6556e8;
}
.small-button.secondary {
  color: #413d51;
  background: #f0eef6;
}
.small-button.danger {
  color: #a24444;
  background: #faeeee;
}
.plan-warning {
  display: block;
  margin-top: 12rpx;
  color: #97651f;
  font-size: 18rpx;
  line-height: 1.45;
}
.plan-result {
  display: block;
  margin-top: 20rpx;
  color: #258768;
  font-size: 20rpx;
  font-weight: 650;
  text-align: right;
}
.plan-result.editing {
  color: #a06a26;
}
button::after {
  border: 0;
}
</style>
