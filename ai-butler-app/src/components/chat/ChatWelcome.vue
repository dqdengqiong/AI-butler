<script setup lang="ts">
import type { AgentShortcutCode, AgentShortcutViewModel } from '@/types/view-models'

defineProps<{
  userName: string
  agents: AgentShortcutViewModel[]
  activeAgent?: AgentShortcutViewModel
  activeAgentCode?: AgentShortcutCode
}>()

defineEmits<{ selectAgent: [agentCode: AgentShortcutCode] }>()
</script>

<template>
  <view class="welcome-stage">
    <view class="butler-mark"
      ><text>{{ activeAgent?.icon ?? '✦' }}</text
      ><view class="online-dot"
    /></view>
    <text class="welcome-kicker">{{
      activeAgent ? `${activeAgent.name}助理` : `下午好，${userName}`
    }}</text>
    <text class="welcome-title">{{
      activeAgent ? '开始你的专属计划' : '今天想让我帮你做什么？'
    }}</text>
    <text class="welcome-copy">{{
      activeAgent?.welcomeMessage ?? '可以聊生活，也可以让我帮你推进计划'
    }}</text>

    <scroll-view v-if="!activeAgent" class="agent-shortcut-scroll" scroll-x :show-scrollbar="false">
      <view class="agent-shortcut-row">
        <button
          v-for="agent in agents"
          :key="agent.code"
          class="agent-shortcut"
          :class="{
            current: agent.code === activeAgentCode,
            coming: agent.availability === 'COMING_SOON',
          }"
          @click="$emit('selectAgent', agent.code)"
        >
          <text class="agent-icon">{{ agent.icon }}</text>
          <view class="agent-copy">
            <view class="agent-heading">
              <text class="agent-name">{{ agent.name }}</text>
              <text v-if="agent.code === activeAgentCode" class="agent-state current">当前</text>
              <text v-else-if="agent.availability === 'COMING_SOON'" class="agent-state"
                >即将开放</text
              >
            </view>
            <text class="agent-description">{{ agent.description }}</text>
          </view>
        </button>
      </view>
    </scroll-view>
  </view>
</template>

<style scoped>
.welcome-stage {
  display: flex;
  align-items: center;
  padding: 78rpx 20rpx 36rpx;
  text-align: center;
  flex-direction: column;
}
.butler-mark {
  position: relative;
  display: flex;
  width: 124rpx;
  height: 124rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 48rpx;
  background: linear-gradient(145deg, #7765ef 8%, #a899ff 58%, #d6d0ff);
  border: 9rpx solid rgba(255, 255, 255, 0.78);
  border-radius: 42rpx;
  box-shadow: 0 24rpx 65rpx rgba(96, 73, 218, 0.28);
  transform: rotate(-4deg);
}
.butler-mark text {
  transform: rotate(4deg);
}
.online-dot {
  position: absolute;
  right: -2rpx;
  bottom: 4rpx;
  width: 22rpx;
  height: 22rpx;
  background: #51c99c;
  border: 6rpx solid #f4f2ff;
  border-radius: 50%;
}
.welcome-kicker {
  margin-top: 36rpx;
  color: #6d6488;
  font-size: 23rpx;
  font-weight: 650;
}
.welcome-title {
  margin-top: 12rpx;
  color: #242137;
  font-size: 42rpx;
  font-weight: 780;
  letter-spacing: -1rpx;
}
.welcome-copy {
  max-width: 590rpx;
  margin-top: 15rpx;
  color: #8c889d;
  font-size: 21rpx;
  line-height: 1.65;
}
.agent-shortcut-scroll {
  width: 100%;
  margin-top: 52rpx;
  white-space: nowrap;
}
.agent-shortcut-row {
  display: flex;
  gap: 12rpx;
  width: max-content;
  padding-right: 24rpx;
}
.agent-shortcut {
  display: flex;
  flex: 0 0 auto;
  width: 246rpx;
  min-height: 88rpx;
  align-items: center;
  gap: 12rpx;
  margin: 0;
  padding: 12rpx 14rpx;
  color: #625d73;
  line-height: 1.2;
  text-align: left;
  background: rgba(255, 255, 255, 0.85);
  border: 1px solid #e4e0f0;
  border-radius: 24rpx;
}
.agent-shortcut.current {
  color: #5144c7;
  background: #ebe7ff;
  border-color: #bcb2ff;
}
.agent-shortcut.coming {
  color: #817d8e;
  background: rgba(248, 247, 252, 0.9);
}
.agent-icon {
  display: flex;
  flex: 0 0 auto;
  width: 52rpx;
  height: 52rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 20rpx;
  font-weight: 760;
  background: linear-gradient(145deg, #6a59df, #9589f5);
  border-radius: 17rpx;
}
.agent-shortcut.coming .agent-icon {
  color: #817b94;
  background: #e9e6ef;
}
.agent-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
}
.agent-heading {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 7rpx;
}
.agent-name {
  font-size: 20rpx;
  font-weight: 720;
}
.agent-state {
  flex: 0 0 auto;
  padding: 4rpx 6rpx;
  color: #898394;
  font-size: 12rpx;
  background: #e9e6ed;
  border-radius: 7rpx;
}
.agent-state.current {
  color: #fff;
  background: #6556e8;
}
.agent-description {
  overflow: hidden;
  margin-top: 7rpx;
  color: #8b8698;
  font-size: 15rpx;
  text-overflow: ellipsis;
  white-space: nowrap;
}
button::after {
  border: 0;
}
</style>
