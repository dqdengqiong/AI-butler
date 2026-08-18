<script setup lang="ts">
defineProps<{
  userName: string
  activePlanCount: number
  completedTaskCount: number
  totalTaskCount: number
}>()

const emit = defineEmits<{
  openSettings: []
  openMaterials: []
  openHistory: []
}>()
</script>

<template>
  <view class="mine-view view-content">
    <view class="profile-card">
      <view class="profile-avatar">{{ userName.slice(0, 1) }}</view>
      <view class="profile-copy">
        <text class="profile-name">{{ userName }}</text>
        <text class="profile-meta">{{ activePlanCount }} 个进行中计划</text>
      </view>
      <text class="profile-arrow">›</text>
    </view>

    <view class="summary-card">
      <view>
        <text class="summary-value">{{ activePlanCount }}</text>
        <text class="summary-label">进行中计划</text>
      </view>
      <view class="divider" />
      <view>
        <text class="summary-value">{{ completedTaskCount }}</text>
        <text class="summary-label">完成任务</text>
      </view>
      <view class="divider" />
      <view>
        <text class="summary-value">{{ totalTaskCount }}</text>
        <text class="summary-label">全部任务</text>
      </view>
    </view>

    <view class="section-label">账户与偏好</view>
    <view class="menu-list">
      <button class="menu-item" @click="emit('openSettings')">
        <view class="menu-icon blue">设</view>
        <view class="menu-copy">
          <text class="menu-title">设置</text>
          <text class="menu-subtitle">提醒、隐私和账号</text>
        </view>
        <text class="menu-arrow">›</text>
      </button>
      <button class="menu-item" @click="emit('openMaterials')">
        <view class="menu-icon green">库</view>
        <view class="menu-copy">
          <text class="menu-title">我的资料</text>
          <text class="menu-subtitle">已上传并可用于检索的学习材料</text>
        </view>
        <text class="menu-arrow">›</text>
      </button>
      <button class="menu-item" @click="emit('openHistory')">
        <view class="menu-icon orange">史</view>
        <view class="menu-copy">
          <text class="menu-title">计划版本</text>
          <text class="menu-subtitle">查看计划调整与确认历史</text>
        </view>
        <text class="menu-arrow">›</text>
      </button>
    </view>

    <view class="privacy-note">
      <text class="privacy-icon">盾</text>
      <view>
        <text class="privacy-title">你的数据默认保持私密</text>
        <text class="privacy-copy"
          >附件经过安全扫描后才会用于当前对话，计划修改始终需要你确认。</text
        >
      </view>
    </view>
  </view>
</template>

<style scoped>
.profile-card {
  display: flex;
  align-items: center;
  gap: 22rpx;
  padding: 30rpx;
  background: #fff;
  border: 1px solid #e4e8f0;
  border-radius: 32rpx;
}

.profile-avatar {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 92rpx;
  height: 92rpx;
  color: #fff;
  font-size: 32rpx;
  font-weight: 800;
  background: linear-gradient(135deg, #596bff, #4ec5a0);
  border-radius: 30rpx;
}

.profile-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 9rpx;
}

.profile-name {
  font-size: 31rpx;
  font-weight: 750;
}

.profile-meta,
.menu-subtitle {
  color: #7c8497;
  font-size: 20rpx;
}

.profile-arrow,
.menu-arrow {
  color: #a2a9b7;
  font-size: 42rpx;
}

.summary-card {
  display: grid;
  grid-template-columns: 1fr 1px 1fr 1px 1fr;
  align-items: center;
  margin-top: 22rpx;
  padding: 28rpx 18rpx;
  background: #202a45;
  border-radius: 30rpx;
}

.summary-card > view:not(.divider) {
  display: flex;
  align-items: center;
  flex-direction: column;
  gap: 7rpx;
}

.summary-value {
  color: #fff;
  font-size: 29rpx;
  font-weight: 750;
}

.summary-label {
  color: #c7cede;
  font-size: 18rpx;
}

.divider {
  width: 1px;
  height: 50rpx;
  background: rgba(255, 255, 255, 0.15);
}

.section-label {
  margin: 38rpx 6rpx 16rpx;
  color: #858da0;
  font-size: 20rpx;
  font-weight: 650;
}

.menu-list {
  overflow: hidden;
  background: #fff;
  border: 1px solid #e4e8f0;
  border-radius: 30rpx;
}

.menu-item {
  display: flex;
  width: 100%;
  min-height: 116rpx;
  align-items: center;
  gap: 20rpx;
  margin: 0;
  padding: 20rpx 26rpx;
  line-height: 1.25;
  text-align: left;
  background: #fff;
  border: 0;
  border-bottom: 1px solid #edf0f4;
  border-radius: 0;
}

.menu-item:last-child {
  border-bottom: 0;
}

.menu-item::after {
  border: 0;
}

.menu-icon {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 68rpx;
  height: 68rpx;
  font-size: 22rpx;
  font-weight: 750;
  border-radius: 22rpx;
}

.menu-icon.blue {
  color: #596bff;
  background: #eef0ff;
}

.menu-icon.green {
  color: #2b9571;
  background: #eaf8f3;
}

.menu-icon.orange {
  color: #d98526;
  background: #fff3e5;
}

.menu-copy {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 8rpx;
}

.menu-title {
  font-size: 24rpx;
  font-weight: 680;
}

.privacy-note {
  display: flex;
  gap: 18rpx;
  margin-top: 24rpx;
  padding: 26rpx;
  background: #effaf6;
  border: 1px solid #d8f0e7;
  border-radius: 28rpx;
}

.privacy-icon {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 54rpx;
  height: 54rpx;
  color: #fff;
  font-size: 17rpx;
  font-weight: 750;
  background: #4ec5a0;
  border-radius: 18rpx;
}

.privacy-note > view {
  display: flex;
  flex-direction: column;
  gap: 7rpx;
}

.privacy-title {
  color: #2b5d4e;
  font-size: 22rpx;
  font-weight: 700;
}

.privacy-copy {
  color: #668579;
  font-size: 19rpx;
  line-height: 1.55;
}
</style>
