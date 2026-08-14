<script setup lang="ts">
import { computed, ref } from 'vue'

import type { PlanViewModel, TaskViewModel } from '@/types/view-models'

const props = defineProps<{
  plans: PlanViewModel[]
  tasks: TaskViewModel[]
}>()

const emit = defineEmits<{
  completeTask: [taskKey: string]
  requestAdjustment: []
}>()

const activeFilter = ref<'all' | 'today' | 'week'>('all')
const doneCount = computed(() => props.tasks.filter((task) => task.done).length)
const totalMinutes = computed(() =>
  props.tasks.filter((task) => !task.done).reduce((sum, task) => sum + task.durationMinutes, 0),
)
</script>

<template>
  <view class="plans-view view-content">
    <view class="filter-row">
      <button
        v-for="filter in [
          { key: 'all', label: '全部' },
          { key: 'today', label: '今天' },
          { key: 'week', label: '本周' },
        ] as const"
        :key="filter.key"
        class="filter-chip"
        :class="{ active: activeFilter === filter.key }"
        @click="activeFilter = filter.key"
      >
        {{ filter.label }}
      </button>
    </view>

    <view class="plan-card">
      <view class="plan-top">
        <view class="plan-icon">{{ plans[0]?.icon ?? '公' }}</view>
        <view class="plan-copy">
          <text class="plan-title">{{ plans[0]?.title ?? '公务员备考' }}</text>
          <text class="plan-subtitle">{{ plans[0]?.subtitle ?? '基础阶段' }} · 已确认</text>
        </view>
        <text class="plan-badge">进行中</text>
      </view>
      <view class="progress-track">
        <view class="progress-value" :style="{ width: `${plans[0]?.progress ?? 0}%` }" />
      </view>
      <view class="plan-meta">
        <text>{{ plans[0]?.progressLabel }}</text>
        <text>{{ plans[0]?.progress }}%</text>
      </view>
    </view>

    <view v-if="totalMinutes > 90" class="schedule-alert">
      <view class="alert-icon">!</view>
      <view class="alert-copy">
        <text class="alert-title">今天剩余任务预计需要 {{ totalMinutes }} 分钟</text>
        <text class="alert-text">如果时间紧张，可以让管家生成单计划调整草案。</text>
      </view>
      <button class="alert-action" @click="emit('requestAdjustment')">让管家调整</button>
    </view>

    <view class="task-card">
      <view class="task-card-header">
        <view>
          <text class="day-label">08月09日 · 今天</text>
          <text class="task-heading">今日任务</text>
        </view>
        <text class="task-count">{{ doneCount }} / {{ tasks.length }} 完成</text>
      </view>

      <view
        v-for="task in tasks"
        :key="task.key"
        class="task-row"
        @click="emit('completeTask', task.key)"
      >
        <view class="checkmark" :class="{ checked: task.done }">{{ task.done ? '✓' : '' }}</view>
        <view class="task-copy">
          <text class="task-title" :class="{ done: task.done }">{{ task.title }}</text>
          <text class="task-meta">{{ task.planTitle }} · {{ task.durationMinutes }} 分钟</text>
        </view>
        <text class="task-tag" :class="task.tone">公考</text>
      </view>
    </view>

    <view class="week-summary">
      <view>
        <text class="summary-label">本周投入</text>
        <text class="summary-value">3.5 小时</text>
      </view>
      <view>
        <text class="summary-label">已完成</text>
        <text class="summary-value">{{ doneCount }} 项</text>
      </view>
      <view>
        <text class="summary-label">距目标</text>
        <text class="summary-value">112 天</text>
      </view>
    </view>

    <view class="policy-note">
      <text class="policy-icon">i</text>
      <text>计划变更需在聊天中结构化确认；已完成任务暂不支持直接撤销。</text>
    </view>
  </view>
</template>

<style scoped>
.filter-row {
  display: flex;
  gap: 14rpx;
  padding: 2rpx 0 22rpx;
}

.filter-chip {
  min-width: 104rpx;
  min-height: 62rpx;
  margin: 0;
  padding: 0 24rpx;
  color: #727b91;
  font-size: 22rpx;
  line-height: 62rpx;
  background: #fff;
  border: 1px solid #e4e8f0;
  border-radius: 999rpx;
}

.filter-chip::after,
.alert-action::after {
  border: 0;
}

.filter-chip.active {
  color: #fff;
  background: #202840;
  border-color: #202840;
}

.plan-card,
.task-card {
  background: #fff;
  border: 1px solid #e5e9f1;
  border-radius: 30rpx;
  box-shadow: 0 10rpx 30rpx rgba(30, 40, 68, 0.04);
}

.plan-card {
  padding: 28rpx;
}

.plan-top {
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
  background: #596bff;
  border-radius: 24rpx;
}

.plan-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 6rpx;
}

.plan-title {
  font-size: 28rpx;
  font-weight: 720;
}

.plan-subtitle,
.plan-meta {
  color: #7a8397;
  font-size: 21rpx;
}

.plan-badge {
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
  background: #596bff;
  border-radius: inherit;
}

.plan-meta {
  display: flex;
  justify-content: space-between;
}

.schedule-alert {
  display: grid;
  grid-template-columns: 52rpx 1fr;
  gap: 14rpx 16rpx;
  margin: 24rpx 0;
  padding: 24rpx;
  background: #fff7eb;
  border: 1px solid #ffe0b7;
  border-radius: 28rpx;
}

.alert-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 50rpx;
  height: 50rpx;
  color: #fff;
  font-size: 24rpx;
  font-weight: 800;
  background: #f5a64a;
  border-radius: 50%;
}

.alert-copy {
  display: flex;
  flex-direction: column;
  gap: 7rpx;
}

.alert-title {
  font-size: 23rpx;
  font-weight: 700;
}

.alert-text {
  color: #8c6e49;
  font-size: 20rpx;
  line-height: 1.5;
}

.alert-action {
  grid-column: 2;
  width: max-content;
  min-height: auto;
  margin: 0;
  padding: 0;
  color: #d98120;
  font-size: 21rpx;
  font-weight: 750;
  line-height: 1.5;
  background: transparent;
}

.task-card {
  padding: 28rpx;
}

.task-card-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  padding-bottom: 10rpx;
}

.task-card-header > view {
  display: flex;
  flex-direction: column;
  gap: 5rpx;
}

.day-label {
  color: #8b93a5;
  font-size: 19rpx;
}

.task-heading {
  font-size: 32rpx;
  font-weight: 760;
}

.task-count {
  color: #727b91;
  font-size: 20rpx;
}

.task-row {
  display: flex;
  min-height: 124rpx;
  align-items: center;
  gap: 18rpx;
  border-bottom: 1px solid #edf0f5;
}

.task-row:last-child {
  border-bottom: 0;
}

.checkmark {
  width: 42rpx;
  height: 42rpx;
  color: #fff;
  font-size: 25rpx;
  line-height: 42rpx;
  text-align: center;
  border: 2px solid #d5dbe6;
  border-radius: 50%;
}

.checkmark.checked {
  background: #4ec5a0;
  border-color: #4ec5a0;
}

.task-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 8rpx;
}

.task-title {
  overflow: hidden;
  font-size: 23rpx;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-title.done {
  color: #9ba2b1;
  text-decoration: line-through;
}

.task-meta {
  color: #7b8497;
  font-size: 19rpx;
}

.task-tag {
  padding: 7rpx 12rpx;
  font-size: 18rpx;
  border-radius: 999rpx;
}

.task-tag.blue {
  color: #596bff;
  background: #eef0ff;
}

.week-summary {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14rpx;
  margin-top: 22rpx;
}

.week-summary > view {
  display: flex;
  flex-direction: column;
  gap: 9rpx;
  padding: 22rpx;
  background: #fff;
  border: 1px solid #e7eaf1;
  border-radius: 24rpx;
}

.summary-label {
  color: #858da0;
  font-size: 19rpx;
}

.summary-value {
  font-size: 26rpx;
  font-weight: 740;
}

.policy-note {
  display: flex;
  align-items: flex-start;
  gap: 14rpx;
  margin-top: 22rpx;
  padding: 22rpx;
  color: #6f788c;
  font-size: 20rpx;
  line-height: 1.55;
  background: #eef1f7;
  border-radius: 22rpx;
}

.policy-icon {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 32rpx;
  height: 32rpx;
  color: #596bff;
  font-size: 19rpx;
  font-weight: 800;
  background: #fff;
  border-radius: 50%;
}
</style>
