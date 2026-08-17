<script setup lang="ts">
import AppSheet from '@/components/AppSheet.vue'

defineProps<{ open: boolean }>()
defineEmits<{ close: []; choose: []; openMaterials: [] }>()
</script>

<template>
  <AppSheet :open="open" eyebrow="添加到当前对话" title="给管家补充上下文" @close="$emit('close')">
    <view class="attachment-grid">
      <button @click="$emit('choose')">
        <text class="attachment-icon">文</text><text class="title">上传文件</text
        ><text class="copy">简历、公告、表格和文档</text>
      </button>
      <button @click="$emit('choose')">
        <text class="attachment-icon purple">图</text><text class="title">选择图片</text
        ><text class="copy">题目、课程或岗位截图</text>
      </button>
      <button @click="$emit('choose')">
        <text class="attachment-icon orange">扫</text><text class="title">拍照 / 扫描</text
        ><text class="copy">错题、通知或学习记录</text>
      </button>
      <button @click="$emit('openMaterials')">
        <text class="attachment-icon green">库</text><text class="title">从资料库选择</text
        ><text class="copy">使用已入库的私有资料</text>
      </button>
    </view>
    <view class="note">文件需完成私有上传与安全扫描后，才能作为 file_id 随消息发送。</view>
  </AppSheet>
</template>

<style scoped>
.attachment-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16rpx;
  margin-top: 30rpx;
}
.attachment-grid button {
  display: flex;
  min-height: 180rpx;
  align-items: flex-start;
  margin: 0;
  padding: 24rpx;
  line-height: 1.35;
  text-align: left;
  background: #f8f9fc;
  border: 1px solid #e2e6ef;
  border-radius: 28rpx;
  flex-direction: column;
}
.attachment-icon {
  width: 56rpx;
  height: 56rpx;
  color: #596bff;
  font-size: 20rpx;
  font-weight: 750;
  line-height: 56rpx;
  text-align: center;
  background: #eef0ff;
  border-radius: 18rpx;
}
.attachment-icon.purple {
  color: #986ee8;
  background: #f3edff;
}
.attachment-icon.orange {
  color: #d98427;
  background: #fff2e3;
}
.attachment-icon.green {
  color: #2f9773;
  background: #e8f7f2;
}
.title {
  margin-top: 14rpx;
  color: #283148;
  font-size: 22rpx;
  font-weight: 700;
}
.copy {
  margin-top: 7rpx;
  color: #7b8497;
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
button::after {
  border: 0;
}
</style>
