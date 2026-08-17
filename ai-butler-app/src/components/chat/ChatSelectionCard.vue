<script setup lang="ts">
import type { ChatItem } from '@/types/view-models'

defineProps<{ item: Extract<ChatItem, { kind: 'selection' }> }>()
defineEmits<{
  selectOption: [itemKey: string, optionIndex: number]
  submit: [itemKey: string]
}>()
</script>

<template>
  <view class="message-card">
    <text class="card-label">{{ item.allowFreeText ? '告诉我你的安排' : '需要你的选择' }}</text>
    <text class="card-title">{{ item.title }}</text>
    <text class="card-description">{{ item.description }}</text>
    <text v-if="item.allowFreeText && !item.submitted" class="natural-input-hint"
      >可直接在下方输入，也可以选择常用安排</text
    >
    <view class="option-grid">
      <button
        v-for="(option, index) in item.options"
        :key="option"
        class="option-button"
        :class="{ active: item.selected === index }"
        :disabled="item.submitted"
        @click="$emit('selectOption', item.key, index)"
      >
        {{ option }}
      </button>
    </view>
    <view class="card-actions">
      <text v-if="item.submitted" class="submitted-label">✓ 已提交</text>
      <button
        v-else
        class="small-button primary"
        :disabled="item.selected < 0"
        @click="$emit('submit', item.key)"
      >
        {{ item.submitLabel }}
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
.natural-input-hint {
  display: block;
  margin-top: 14rpx;
  padding: 14rpx 16rpx;
  color: #6556e8;
  font-size: 18rpx;
  background: #f2efff;
  border-radius: 16rpx;
}
.option-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12rpx;
  margin-top: 22rpx;
}
.option-button {
  min-height: 72rpx;
  margin: 0;
  padding: 0 14rpx;
  color: #4f4a63;
  font-size: 20rpx;
  line-height: 1.35;
  background: #f7f6fc;
  border: 1px solid #e5e1f2;
  border-radius: 20rpx;
}
.option-button.active {
  color: #6556e8;
  font-weight: 700;
  background: #eeebff;
  border-color: #b7abff;
}
.card-actions {
  display: flex;
  justify-content: flex-end;
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
.small-button.primary[disabled] {
  color: #aaa5bd;
  background: #e8e5ef;
}
.submitted-label {
  color: #258768;
  font-size: 20rpx;
  font-weight: 650;
}
button::after {
  border: 0;
}
</style>
