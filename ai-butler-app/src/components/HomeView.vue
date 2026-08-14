<script setup lang="ts">
import { computed } from 'vue'

import type { PlanViewModel, TaskViewModel } from '@/types/view-models'

const props = defineProps<{
  plans: PlanViewModel[]
  tasks: TaskViewModel[]
}>()

const emit = defineEmits<{
  navigate: [target: 'plans' | 'chat']
}>()

const completedCount = computed(() => props.tasks.filter((task) => task.done).length)
const pendingCount = computed(() => props.tasks.length - completedCount.value)
</script>

<template>
  <view class="home-view view-content">
    <view class="manager-card">
      <view class="manager-avatar">管</view>
      <view class="manager-copy">
        <view class="manager-heading">
          <view>
            <text class="manager-title">小管家</text>
            <text class="online-label"><text class="online-dot" />在线</text>
          </view>
          <text class="manager-caption">唯一沟通入口</text>
        </view>
        <text class="manager-summary">
          今天还有 {{ pendingCount }} 项任务，我会根据你的进度及时帮你调整节奏。
        </text>
        <button class="light-button" @click="emit('navigate', 'chat')">和管家聊聊</button>
      </view>
    </view>

    <view class="stats-row">
      <view class="stat-card">
        <text class="stat-value">{{ plans.length }}</text>
        <text class="stat-label">进行中计划</text>
      </view>
      <view class="stat-card accent">
        <text class="stat-value">{{ completedCount }}/{{ tasks.length }}</text>
        <text class="stat-label">今日任务</text>
      </view>
      <view class="stat-card">
        <text class="stat-value">38%</text>
        <text class="stat-label">本周进度</text>
      </view>
    </view>

    <view class="section-heading">
      <view>
        <text class="section-eyebrow">正在推进</text>
        <text class="section-title">我的计划</text>
      </view>
      <button class="text-button" @click="emit('navigate', 'plans')">查看全部</button>
    </view>

    <view class="plan-stack">
      <view v-for="plan in plans" :key="plan.key" class="plan-card">
        <view class="plan-card-top">
          <view class="plan-icon" :class="plan.tone">{{ plan.icon }}</view>
          <view class="plan-copy">
            <text class="plan-title">{{ plan.title }}</text>
            <text class="plan-subtitle">{{ plan.subtitle }}</text>
          </view>
          <text class="status-pill">{{ plan.statusLabel }}</text>
        </view>
        <view class="progress-track">
          <view class="progress-value" :class="plan.tone" :style="{ width: `${plan.progress}%` }" />
        </view>
        <view class="plan-meta">
          <text>{{ plan.progressLabel }}</text>
          <text>{{ plan.progress }}%</text>
        </view>
      </view>
    </view>

    <view class="section-heading compact">
      <view>
        <text class="section-eyebrow">今天</text>
        <text class="section-title">还要做什么</text>
      </view>
      <button class="text-button" @click="emit('navigate', 'plans')">管理任务</button>
    </view>

    <view class="today-card">
      <view v-for="task in tasks.slice(0, 3)" :key="task.key" class="today-row">
        <view class="today-state" :class="{ done: task.done }">{{ task.done ? '✓' : '' }}</view>
        <view class="today-copy">
          <text class="today-title" :class="{ crossed: task.done }">{{ task.title }}</text>
          <text class="today-meta">{{ task.planTitle }} · {{ task.durationMinutes }} 分钟</text>
        </view>
        <text class="today-arrow">›</text>
      </view>
    </view>
  </view>
</template>

<style scoped>
.manager-card {
  display: flex;
  gap: 24rpx;
  padding: 34rpx;
  color: #fff;
  background: linear-gradient(145deg, #202a45 0%, #344773 100%);
  border-radius: 36rpx;
  box-shadow: 0 24rpx 56rpx rgba(26, 37, 66, 0.17);
}

.manager-avatar {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 92rpx;
  height: 92rpx;
  font-size: 34rpx;
  font-weight: 800;
  background: linear-gradient(135deg, #7180ff, #50c7a3);
  border-radius: 30rpx;
}

.manager-copy,
.manager-copy > text,
.manager-heading > view {
  display: flex;
  flex-direction: column;
}

.manager-copy {
  min-width: 0;
  flex: 1;
}

.manager-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
}

.manager-title {
  font-size: 32rpx;
  font-weight: 750;
}

.online-label {
  display: flex;
  align-items: center;
  gap: 8rpx;
  margin-top: 4rpx;
  color: #bfe8da;
  font-size: 21rpx;
}

.online-dot {
  width: 12rpx;
  height: 12rpx;
  background: #4ec5a0;
  border-radius: 50%;
}

.manager-caption {
  color: #cbd2e3;
  font-size: 20rpx;
}

.manager-summary {
  margin: 20rpx 0 24rpx;
  color: #e3e7f1;
  font-size: 24rpx;
  line-height: 1.65;
}

.light-button {
  width: max-content;
  min-height: 68rpx;
  margin: 0;
  padding: 0 28rpx;
  color: #28324e;
  font-size: 24rpx;
  font-weight: 700;
  line-height: 68rpx;
  background: #fff;
  border: 0;
  border-radius: 22rpx;
}

.light-button::after,
.text-button::after {
  border: 0;
}

.stats-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14rpx;
  margin-top: 24rpx;
}

.stat-card {
  display: flex;
  flex-direction: column;
  gap: 6rpx;
  padding: 24rpx 20rpx;
  background: #fff;
  border: 1px solid #e8ebf2;
  border-radius: 26rpx;
}

.stat-card.accent {
  background: #eef0ff;
  border-color: #dfe3ff;
}

.stat-value {
  color: #202840;
  font-size: 34rpx;
  font-weight: 780;
}

.stat-label {
  color: #7b8498;
  font-size: 20rpx;
}

.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  margin: 42rpx 4rpx 20rpx;
}

.section-heading > view {
  display: flex;
  flex-direction: column;
  gap: 6rpx;
}

.section-heading.compact {
  margin-top: 38rpx;
}

.section-eyebrow {
  color: #8991a3;
  font-size: 19rpx;
  font-weight: 650;
  letter-spacing: 2rpx;
}

.section-title {
  color: #192139;
  font-size: 36rpx;
  font-weight: 760;
}

.text-button {
  margin: 0;
  padding: 0;
  color: #596bff;
  font-size: 23rpx;
  font-weight: 700;
  line-height: 1.5;
  background: transparent;
}

.plan-stack {
  display: grid;
  gap: 18rpx;
}

.plan-card,
.today-card {
  background: #fff;
  border: 1px solid #e5e9f1;
  border-radius: 30rpx;
  box-shadow: 0 10rpx 30rpx rgba(30, 40, 68, 0.04);
}

.plan-card {
  padding: 28rpx;
}

.plan-card-top {
  display: flex;
  align-items: center;
  gap: 20rpx;
}

.plan-icon {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 76rpx;
  height: 76rpx;
  color: #fff;
  font-size: 27rpx;
  font-weight: 800;
  border-radius: 24rpx;
}

.plan-icon.blue,
.progress-value.blue {
  background: #596bff;
}

.plan-icon.purple,
.progress-value.purple {
  background: #986ee8;
}

.plan-icon.green,
.progress-value.green {
  background: #4ba97c;
}

.plan-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 5rpx;
}

.plan-title {
  font-size: 27rpx;
  font-weight: 700;
}

.plan-subtitle,
.plan-meta {
  color: #7b8498;
  font-size: 21rpx;
}

.status-pill {
  padding: 9rpx 16rpx;
  color: #596bff;
  font-size: 19rpx;
  background: #eef0ff;
  border-radius: 999rpx;
}

.progress-track {
  height: 12rpx;
  margin: 25rpx 0 12rpx;
  overflow: hidden;
  background: #eef0f5;
  border-radius: 99rpx;
}

.progress-value {
  height: 100%;
  border-radius: inherit;
}

.plan-meta {
  display: flex;
  justify-content: space-between;
}

.today-card {
  padding: 0 26rpx;
  overflow: hidden;
}

.today-row {
  display: flex;
  min-height: 122rpx;
  align-items: center;
  gap: 18rpx;
  border-bottom: 1px solid #edf0f5;
}

.today-row:last-child {
  border-bottom: 0;
}

.today-state {
  width: 38rpx;
  height: 38rpx;
  color: #fff;
  font-size: 24rpx;
  line-height: 38rpx;
  text-align: center;
  border: 2px solid #d5dbe6;
  border-radius: 50%;
}

.today-state.done {
  background: #4ec5a0;
  border-color: #4ec5a0;
}

.today-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 8rpx;
}

.today-title {
  overflow: hidden;
  font-size: 24rpx;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.today-title.crossed {
  color: #9aa1b0;
  text-decoration: line-through;
}

.today-meta {
  color: #838b9d;
  font-size: 20rpx;
}

.today-arrow {
  color: #aab0bd;
  font-size: 36rpx;
}
</style>
