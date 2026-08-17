<script setup lang="ts">
import AppSheet from '@/components/AppSheet.vue'
import type { AgentShortcutCode, AgentShortcutViewModel } from '@/types/view-models'

defineProps<{
  open: boolean
  agents: AgentShortcutViewModel[]
  isCurrent: (code: AgentShortcutCode | null) => boolean
  statusFor: (code: AgentShortcutCode | null) => string | null
}>()
defineEmits<{ close: []; select: [code: AgentShortcutCode | null] }>()
</script>

<template>
  <AppSheet :open="open" eyebrow="当前场景" title="切换助理" @close="$emit('close')">
    <view class="assistant-picker">
      <text class="assistant-group-label">通用</text>
      <button
        class="assistant-option"
        :class="{ current: isCurrent(null) }"
        :aria-pressed="isCurrent(null)"
        @click="$emit('select', null)"
      >
        <text class="assistant-option-icon general">✦</text>
        <view class="assistant-option-copy">
          <text class="assistant-option-name">AI 管家</text>
          <text class="assistant-option-description">处理日常问题、资料和跨领域计划</text>
        </view>
        <text v-if="isCurrent(null)" class="assistant-option-state current">当前</text>
        <text v-else-if="statusFor(null)" class="assistant-option-state pending">{{
          statusFor(null)
        }}</text>
        <text v-else class="assistant-option-arrow">›</text>
      </button>
      <text class="assistant-group-label specialists">专业助理</text>
      <button
        v-for="agent in agents"
        :key="agent.code"
        class="assistant-option"
        :class="{
          current: isCurrent(agent.code),
          unavailable: agent.availability === 'COMING_SOON',
        }"
        :disabled="agent.availability === 'COMING_SOON'"
        :aria-pressed="isCurrent(agent.code)"
        @click="$emit('select', agent.code)"
      >
        <text class="assistant-option-icon">{{ agent.icon }}</text>
        <view class="assistant-option-copy">
          <text class="assistant-option-name">{{ agent.name }}助理</text>
          <text class="assistant-option-description">{{ agent.description }}</text>
        </view>
        <text v-if="isCurrent(agent.code)" class="assistant-option-state current">当前</text>
        <text
          v-else-if="agent.availability === 'COMING_SOON'"
          class="assistant-option-state unavailable"
          >即将开放</text
        >
        <text v-else-if="statusFor(agent.code)" class="assistant-option-state pending">{{
          statusFor(agent.code)
        }}</text>
        <text v-else class="assistant-option-arrow">›</text>
      </button>
    </view>
  </AppSheet>
</template>

<style scoped>
.assistant-picker {
  display: flex;
  margin-top: 28rpx;
  flex-direction: column;
}
.assistant-group-label {
  margin: 0 5rpx 10rpx;
  color: #918ca1;
  font-size: 18rpx;
  font-weight: 700;
}
.assistant-group-label.specialists {
  margin-top: 26rpx;
}
.assistant-option {
  display: flex;
  min-height: 108rpx;
  align-items: center;
  gap: 18rpx;
  margin: 0 0 12rpx;
  padding: 17rpx 18rpx;
  color: #302d42;
  line-height: 1.25;
  text-align: left;
  background: #f7f6fc;
  border: 1px solid #ebe8f4;
  border-radius: 25rpx;
}
.assistant-option.current {
  background: #efecff;
  border-color: #c8bfff;
}
.assistant-option.unavailable {
  opacity: 0.62;
}
.assistant-option-icon {
  display: flex;
  flex: 0 0 auto;
  width: 68rpx;
  height: 68rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 28rpx;
  font-weight: 800;
  background: linear-gradient(145deg, #7666ed, #a79cf8);
  border-radius: 22rpx;
  box-shadow: 0 8rpx 20rpx rgba(86, 67, 190, 0.16);
}
.assistant-option-icon.general {
  background: linear-gradient(145deg, #4e68dc, #8498f4);
}
.assistant-option-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 7rpx;
}
.assistant-option-name {
  overflow: hidden;
  color: #302d42;
  font-size: 23rpx;
  font-weight: 750;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-option-description {
  overflow: hidden;
  color: #898498;
  font-size: 18rpx;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assistant-option-state {
  flex: 0 0 auto;
  padding: 8rpx 12rpx;
  font-size: 16rpx;
  font-weight: 700;
  border-radius: 999rpx;
}
.assistant-option-state.current {
  color: #5d4fd5;
  background: #ded8ff;
}
.assistant-option-state.pending {
  color: #9a651e;
  background: #fff0d8;
}
.assistant-option-state.unavailable {
  color: #777284;
  background: #ebe9ef;
}
.assistant-option-arrow {
  flex: 0 0 auto;
  color: #aaa5b6;
  font-size: 34rpx;
}
button::after {
  border: 0;
}
</style>
