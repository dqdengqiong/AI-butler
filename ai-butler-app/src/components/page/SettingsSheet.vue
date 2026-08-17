<script setup lang="ts">
import AppSheet from '@/components/AppSheet.vue'

defineProps<{ open: boolean; loggedIn: boolean; remindersEnabled: boolean }>()
defineEmits<{
  close: []
  openMaterials: []
  openHistory: []
  updateReminders: [enabled: boolean]
  deleteAccount: []
  logout: []
}>()
</script>

<template>
  <AppSheet :open="open" eyebrow="偏好与隐私" title="设置" tall @close="$emit('close')">
    <view class="settings-list">
      <button class="setting-row setting-button" @click="$emit('openMaterials')">
        <view><text>我的资料</text><text>管理聊天可调用的私有资料</text></view
        ><text class="setting-arrow">›</text>
      </button>
      <button class="setting-row setting-button" @click="$emit('openHistory')">
        <view><text>计划版本</text><text>查看调整记录与审批留痕</text></view
        ><text class="setting-arrow">›</text>
      </button>
      <view class="setting-row">
        <view><text>任务提醒</text><text>计划开始前提醒</text></view>
        <switch
          :checked="remindersEnabled"
          color="#596bff"
          @change="$emit('updateReminders', !remindersEnabled)"
        />
      </view>
      <view class="setting-row">
        <view><text>计划变更需确认</text><text>安全策略，无法关闭</text></view>
        <switch checked disabled color="#596bff" />
      </view>
    </view>
    <view v-if="loggedIn" class="danger-zone">
      <button @click="$emit('deleteAccount')">注销账号</button>
      <button class="logout" @click="$emit('logout')">退出登录</button>
      <text>账号注销会撤销会话，并进入服务端异步删除流程。</text>
    </view>
  </AppSheet>
</template>

<style scoped>
.settings-list {
  margin-top: 30rpx;
}
.setting-row {
  display: flex;
  min-height: 114rpx;
  align-items: center;
  justify-content: space-between;
  gap: 22rpx;
  border-bottom: 1px solid #e6e9ef;
}
.setting-button {
  width: 100%;
  margin: 0;
  padding: 0;
  line-height: 1.25;
  text-align: left;
  background: transparent;
  border-radius: 0;
}
.setting-arrow {
  color: #aaa5b5;
  font-size: 34rpx;
}
.setting-row > view {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 8rpx;
}
.setting-row > view > text:first-child {
  font-size: 23rpx;
  font-weight: 680;
}
.setting-row > view > text:last-child {
  color: #7c8496;
  font-size: 18rpx;
}
.danger-zone {
  display: grid;
  gap: 14rpx;
  margin-top: 34rpx;
}
.danger-zone button {
  min-height: 76rpx;
  color: #d84b55;
  font-size: 22rpx;
  line-height: 76rpx;
  background: #fff0f1;
  border: 0;
  border-radius: 22rpx;
}
.danger-zone button.logout {
  color: #384157;
  background: #f0f2f6;
}
.danger-zone > text {
  color: #858da0;
  font-size: 18rpx;
  line-height: 1.55;
  text-align: center;
}
button::after {
  border: 0;
}
</style>
