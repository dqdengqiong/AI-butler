<script setup lang="ts">
import AppSheet from '@/components/AppSheet.vue'
import type { ApiObject } from '@/api/butler'

defineProps<{ open: boolean; items: ApiObject[] }>()
defineEmits<{ close: [] }>()
</script>

<template>
  <AppSheet :open="open" eyebrow="审批留痕" title="计划版本" @close="$emit('close')">
    <view v-if="items.length" class="settings-list">
      <view v-for="item in items" :key="String(item.id)" class="setting-row">
        <view
          ><text>版本 {{ item.revision }}</text
          ><text>{{ item.objective_summary }}</text></view
        >
        <text>{{ item.status }}</text>
      </view>
    </view>
    <view v-else class="note">暂无计划版本。</view>
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
.note {
  margin-top: 22rpx;
  padding: 20rpx;
  color: #757e91;
  font-size: 19rpx;
  line-height: 1.6;
  background: #f2f4f8;
  border-radius: 20rpx;
}
</style>
