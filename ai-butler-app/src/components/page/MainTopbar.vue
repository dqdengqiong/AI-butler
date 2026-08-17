<script setup lang="ts">
import type { AgentShortcutViewModel, MainTab } from '@/types/view-models'

defineProps<{
  activeTab: MainTab
  activeAgent?: AgentShortcutViewModel
  assistantSubtitle: string
  assistantsOpen: boolean
}>()
defineEmits<{
  navigate: [tab: MainTab]
  openDrawer: []
  openAssistants: []
  openSettings: []
}>()
</script>

<template>
  <view class="topbar">
    <button
      class="topbar-icon menu-button"
      :aria-label="activeTab === 'plans' ? '返回聊天' : '打开历史对话'"
      @click="activeTab === 'plans' ? $emit('navigate', 'chat') : $emit('openDrawer')"
    >
      {{ activeTab === 'plans' ? '‹' : '☰' }}
    </button>
    <view v-if="activeTab === 'plans'" class="topbar-title">
      <text class="page-title">计划</text><text class="page-subtitle">目标、任务与进度</text>
    </view>
    <button
      v-else
      class="topbar-title assistant-switch-trigger"
      aria-label="切换 AI 管家或专业助理"
      :aria-expanded="assistantsOpen"
      @click="$emit('openAssistants')"
    >
      <view class="assistant-title-row">
        <text class="assistant-title-icon">{{ activeAgent?.icon ?? '✦' }}</text>
        <text class="page-title">{{ activeAgent ? `${activeAgent.name}助理` : 'AI 管家' }}</text>
        <text class="assistant-chevron">⌄</text>
      </view>
      <text class="page-subtitle">{{ assistantSubtitle }}</text>
    </button>
    <view class="topbar-actions">
      <button
        class="topbar-icon"
        :class="{ active: activeTab === 'plans' }"
        aria-label="打开计划"
        @click="$emit('navigate', activeTab === 'plans' ? 'chat' : 'plans')"
      >
        <text class="header-icon">✓</text><text class="header-label">计划</text>
      </button>
      <button class="topbar-icon" aria-label="打开设置" @click="$emit('openSettings')">
        <text class="header-icon">⚙</text><text class="header-label">设置</text>
      </button>
    </view>
  </view>
</template>

<style scoped>
.topbar {
  position: sticky;
  z-index: 15;
  top: 0;
  display: flex;
  align-items: center;
  gap: 12rpx;
  padding: calc(22rpx + env(safe-area-inset-top)) 22rpx 18rpx;
  background: rgba(244, 242, 255, 0.88);
  border-bottom: 1px solid rgba(100, 82, 174, 0.08);
  backdrop-filter: blur(18px);
}
.topbar-title {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 3rpx;
}
.assistant-switch-trigger {
  align-items: flex-start;
  justify-content: center;
  min-height: 72rpx;
  margin: 0;
  padding: 0 8rpx;
  line-height: 1.2;
  text-align: left;
  background: transparent;
  border: 0;
  border-radius: 18rpx;
}
.assistant-switch-trigger:active {
  background: rgba(101, 86, 232, 0.08);
}
.assistant-title-row {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8rpx;
}
.assistant-title-icon {
  display: flex;
  flex: 0 0 auto;
  width: 34rpx;
  height: 34rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 18rpx;
  font-weight: 800;
  background: linear-gradient(145deg, #6d5be5, #9d91f8);
  border-radius: 11rpx;
}
.assistant-chevron {
  flex: 0 0 auto;
  color: #817b94;
  font-size: 23rpx;
  transform: translateY(-2rpx);
}
.page-title {
  overflow: hidden;
  color: #29263b;
  font-size: 28rpx;
  font-weight: 760;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.page-subtitle {
  color: #918ca1;
  font-size: 17rpx;
}
.topbar-actions {
  display: flex;
  flex: 0 0 auto;
  gap: 7rpx;
}
.topbar-icon {
  display: flex;
  width: 72rpx;
  height: 72rpx;
  align-items: center;
  justify-content: center;
  margin: 0;
  padding: 0;
  color: #555066;
  line-height: 1.1;
  background: rgba(255, 255, 255, 0.7);
  border: 1px solid rgba(97, 78, 174, 0.1);
  border-radius: 23rpx;
  flex-direction: column;
}
.topbar-icon.active {
  color: #6556e8;
  background: #eae6ff;
}
.menu-button {
  flex: 0 0 auto;
  color: #343044;
  font-size: 31rpx;
  font-weight: 750;
  background: transparent;
  border-color: transparent;
}
.header-icon {
  font-size: 22rpx;
  font-weight: 800;
}
.header-label {
  margin-top: 3rpx;
  font-size: 14rpx;
  font-weight: 650;
}
button::after {
  border: 0;
}
</style>
