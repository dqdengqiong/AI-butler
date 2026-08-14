<script setup lang="ts">
import { computed } from 'vue'

import type { AgentShortcutViewModel, ConversationViewModel } from '@/types/view-models'

const props = defineProps<{
  open: boolean
  userName: string
  activeKey: string
  conversations: ConversationViewModel[]
  agentShortcuts: AgentShortcutViewModel[]
}>()

const emit = defineEmits<{
  close: []
  create: []
  select: [conversationKey: string]
  openMaterials: []
}>()

const sections = computed(() => [
  {
    key: 'today' as const,
    label: '今天',
    items: props.conversations.filter((item) => item.section === 'today'),
  },
  {
    key: 'week' as const,
    label: '过去 7 天',
    items: props.conversations.filter((item) => item.section === 'week'),
  },
  {
    key: 'earlier' as const,
    label: '更早',
    items: props.conversations.filter((item) => item.section === 'earlier'),
  },
])

function agentFor(conversation: ConversationViewModel): AgentShortcutViewModel | undefined {
  return props.agentShortcuts.find((agent) => agent.code === conversation.agentCode)
}
</script>

<template>
  <view v-if="open" class="drawer-layer">
    <view class="drawer-mask" @click="emit('close')" />
    <view class="drawer-panel">
      <view class="drawer-safe-area" />
      <view class="drawer-profile">
        <view class="profile-avatar">{{ userName.slice(0, 1) }}</view>
        <view class="profile-copy">
          <text class="profile-name">{{ userName }}</text>
          <text class="profile-caption">AI 管家一直在线</text>
        </view>
        <button class="close-button" aria-label="关闭会话列表" @click="emit('close')">×</button>
      </view>

      <button class="new-chat-button" @click="emit('create')">
        <text class="new-chat-icon">＋</text>
        <view>
          <text>开启新对话</text>
          <text>当前对话将自动归档</text>
        </view>
      </button>

      <scroll-view class="conversation-scroll" scroll-y>
        <view v-for="section in sections" :key="section.key" class="conversation-section">
          <text v-if="section.items.length" class="section-label">{{ section.label }}</text>
          <button
            v-for="conversation in section.items"
            :key="conversation.key"
            class="conversation-row"
            :class="{ active: conversation.key === activeKey }"
            @click="emit('select', conversation.key)"
          >
            <text v-if="agentFor(conversation)" class="conversation-agent-icon">
              {{ agentFor(conversation)?.icon }}
            </text>
            <view class="conversation-copy">
              <view class="conversation-title-row">
                <text class="conversation-title">{{ conversation.title }}</text>
                <text v-if="agentFor(conversation)" class="agent-label">
                  {{ agentFor(conversation)?.name }}
                </text>
                <text v-if="conversation.archived" class="archive-dot">已归档</text>
              </view>
              <text class="conversation-preview">{{ conversation.preview }}</text>
            </view>
            <text class="conversation-time">{{ conversation.timeLabel }}</text>
          </button>
        </view>
      </scroll-view>

      <view class="drawer-footer">
        <button @click="emit('openMaterials')"><text>▣</text>我的资料</button>
        <view class="archive-note"><text>✓</text>对话会在开启新话题后自动归档</view>
      </view>
    </view>
  </view>
</template>

<style scoped>
.drawer-layer {
  position: fixed;
  z-index: 80;
  inset: 0;
}

.drawer-mask {
  position: absolute;
  inset: 0;
  background: rgba(24, 25, 40, 0.42);
  backdrop-filter: blur(8rpx);
}

.drawer-panel {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  display: flex;
  box-sizing: border-box;
  width: min(670rpx, 88vw);
  padding: 0 24rpx calc(24rpx + env(safe-area-inset-bottom));
  background: #f8f8ff;
  border-radius: 0 42rpx 42rpx 0;
  box-shadow: 30rpx 0 90rpx rgba(34, 30, 83, 0.2);
  flex-direction: column;
  animation: drawer-in 0.24s ease-out;
}

.drawer-safe-area {
  height: calc(32rpx + env(safe-area-inset-top));
}

.drawer-profile {
  display: flex;
  align-items: center;
  gap: 18rpx;
  padding: 8rpx 8rpx 28rpx;
}

.profile-avatar {
  display: flex;
  width: 80rpx;
  height: 80rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 29rpx;
  font-weight: 800;
  background: linear-gradient(145deg, #7b6cf6, #a99cff);
  border-radius: 28rpx;
  box-shadow: 0 12rpx 30rpx rgba(105, 86, 223, 0.25);
}

.profile-copy,
.new-chat-button > view,
.conversation-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
}

.profile-name {
  color: #242337;
  font-size: 28rpx;
  font-weight: 750;
}

.profile-caption {
  margin-top: 5rpx;
  color: #89879c;
  font-size: 19rpx;
}

.close-button {
  width: 58rpx;
  height: 58rpx;
  margin: 0;
  padding: 0;
  color: #69677b;
  font-size: 38rpx;
  line-height: 56rpx;
  background: #eeeef8;
  border: 0;
  border-radius: 19rpx;
}

.new-chat-button {
  display: flex;
  min-height: 104rpx;
  align-items: center;
  gap: 18rpx;
  margin: 0 0 28rpx;
  padding: 18rpx 22rpx;
  color: #fff;
  line-height: 1.25;
  text-align: left;
  background: linear-gradient(135deg, #6556e8, #8a79f6);
  border: 0;
  border-radius: 28rpx;
  box-shadow: 0 16rpx 36rpx rgba(96, 76, 220, 0.24);
}

.new-chat-icon {
  display: flex;
  width: 60rpx;
  height: 60rpx;
  align-items: center;
  justify-content: center;
  font-size: 35rpx;
  background: rgba(255, 255, 255, 0.17);
  border-radius: 20rpx;
}

.new-chat-button view text:first-child {
  font-size: 25rpx;
  font-weight: 750;
}

.new-chat-button view text:last-child {
  margin-top: 7rpx;
  color: rgba(255, 255, 255, 0.72);
  font-size: 18rpx;
}

.conversation-scroll {
  min-height: 0;
  flex: 1;
}

.conversation-section {
  margin-bottom: 28rpx;
}

.section-label {
  display: block;
  margin: 0 10rpx 10rpx;
  color: #9693a7;
  font-size: 18rpx;
  font-weight: 650;
}

.conversation-row {
  display: flex;
  width: 100%;
  min-height: 100rpx;
  align-items: flex-start;
  gap: 10rpx;
  margin: 0 0 6rpx;
  padding: 18rpx 16rpx;
  line-height: 1.25;
  text-align: left;
  background: transparent;
  border: 0;
  border-radius: 24rpx;
}

.conversation-agent-icon {
  display: flex;
  flex: 0 0 auto;
  width: 48rpx;
  height: 48rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 18rpx;
  font-weight: 750;
  background: linear-gradient(145deg, #6857dc, #9487ef);
  border-radius: 16rpx;
}

.conversation-row.active {
  background: #eae8ff;
}

.conversation-title-row {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10rpx;
}

.conversation-title {
  overflow: hidden;
  color: #2c2a3d;
  font-size: 23rpx;
  font-weight: 680;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.archive-dot {
  flex: 0 0 auto;
  padding: 5rpx 8rpx;
  color: #77718d;
  font-size: 14rpx;
  background: #e9e7ef;
  border-radius: 8rpx;
}

.agent-label {
  flex: 0 0 auto;
  padding: 5rpx 8rpx;
  color: #6556d8;
  font-size: 14rpx;
  background: #e8e4ff;
  border-radius: 8rpx;
}

.conversation-preview {
  overflow: hidden;
  margin-top: 9rpx;
  color: #858296;
  font-size: 18rpx;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-time {
  flex: 0 0 auto;
  color: #aaa7b7;
  font-size: 16rpx;
}

.drawer-footer {
  padding-top: 18rpx;
  border-top: 1px solid #e6e4ef;
}

.drawer-footer button {
  display: flex;
  width: 100%;
  min-height: 72rpx;
  align-items: center;
  gap: 16rpx;
  margin: 0;
  padding: 0 12rpx;
  color: #454256;
  font-size: 22rpx;
  text-align: left;
  background: transparent;
  border: 0;
}

.archive-note {
  display: flex;
  align-items: center;
  gap: 10rpx;
  padding: 14rpx 12rpx 0;
  color: #9692a4;
  font-size: 16rpx;
}

.archive-note text {
  color: #6556e8;
}

button::after {
  border: 0;
}

@keyframes drawer-in {
  from {
    transform: translateX(-100%);
  }
  to {
    transform: translateX(0);
  }
}
</style>
