<script setup lang="ts">
defineProps<{
  open: boolean
  title: string
  eyebrow: string
  tall?: boolean
}>()

const emit = defineEmits<{
  close: []
}>()
</script>

<template>
  <view v-if="open" class="sheet-backdrop" @click.self="emit('close')">
    <view class="sheet-panel" :class="{ tall }">
      <view class="sheet-handle" />
      <view class="sheet-header">
        <view>
          <text class="sheet-eyebrow">{{ eyebrow }}</text>
          <text class="sheet-title">{{ title }}</text>
        </view>
        <button class="close-button" aria-label="关闭" @click="emit('close')">×</button>
      </view>
      <slot />
    </view>
  </view>
</template>

<style scoped>
.sheet-backdrop {
  position: fixed;
  z-index: 80;
  inset: 0;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  background: rgba(17, 23, 39, 0.5);
}

.sheet-panel {
  box-sizing: border-box;
  width: min(430px, 100%);
  max-height: 84vh;
  padding: 16rpx 36rpx calc(38rpx + env(safe-area-inset-bottom));
  overflow-y: auto;
  background: #fff;
  border-radius: 42rpx 42rpx 0 0;
  box-shadow: 0 -30rpx 70rpx rgba(16, 23, 42, 0.2);
}

.sheet-panel.tall {
  min-height: 68vh;
}

.sheet-handle {
  width: 84rpx;
  height: 9rpx;
  margin: 0 auto 26rpx;
  background: #d8dde7;
  border-radius: 99rpx;
}

.sheet-header,
.sheet-header > view {
  display: flex;
}

.sheet-header {
  align-items: flex-start;
  justify-content: space-between;
  gap: 22rpx;
}

.sheet-header > view {
  flex-direction: column;
  gap: 7rpx;
}

.sheet-eyebrow {
  color: #858da0;
  font-size: 19rpx;
  font-weight: 700;
  letter-spacing: 2rpx;
}

.sheet-title {
  font-size: 34rpx;
  font-weight: 760;
}

.close-button {
  width: 60rpx;
  height: 60rpx;
  margin: 0;
  padding: 0;
  color: #626b7e;
  font-size: 34rpx;
  line-height: 60rpx;
  background: #f1f3f7;
  border: 0;
  border-radius: 50%;
}

.close-button::after {
  border: 0;
}
</style>
