<script setup lang="ts">
import { computed, ref } from 'vue'

import SafeMarkdown from '@/components/SafeMarkdown.vue'
import type {
  AgentShortcutCode,
  AgentShortcutViewModel,
  ChatItem,
  UploadedAttachment,
} from '@/types/view-models'

type PlanChatItem = Extract<ChatItem, { kind: 'plan' }>
type SelectionChatItem = Extract<ChatItem, { kind: 'selection' }>

const props = defineProps<{
  items: ChatItem[]
  attachments: UploadedAttachment[]
  userName: string
  agentShortcuts: AgentShortcutViewModel[]
  activeAgentCode?: AgentShortcutCode
}>()

const emit = defineEmits<{
  send: [content: string]
  openAttachments: []
  removeAttachment: [fileId: string]
  selectOption: [itemKey: string, optionIndex: number]
  submitSelection: [itemKey: string]
  approvePlan: [item: PlanChatItem]
  editPlan: [item: PlanChatItem]
  openSource: [citationId: string]
  selectAgent: [agentCode: AgentShortcutCode]
}>()

const draft = ref('')
const isRecording = ref(false)
const canSend = computed(() => draft.value.trim().length > 0 || props.attachments.length > 0)
const isFreshConversation = computed(
  () => !props.items.some((item) => item.kind === 'message' && item.role === 'user'),
)
const activeAgent = computed(() =>
  props.agentShortcuts.find((agent) => agent.code === props.activeAgentCode),
)
const activeNaturalLanguagePrompt = computed<SelectionChatItem | undefined>(() => {
  for (let index = props.items.length - 1; index >= 0; index -= 1) {
    const item = props.items[index]
    if (!item) continue
    if (item.kind === 'selection' && item.allowFreeText && !item.submitted) return item
  }
  return undefined
})
const composerPlaceholder = computed(() =>
  isRecording.value
    ? '正在聆听…'
    : activeNaturalLanguagePrompt.value?.inputPlaceholder || '发消息或按住说话…',
)

const quickPrompts = computed(() => {
  if (activeAgent.value) {
    return activeAgent.value.starterPrompts.map((prompt) => ({
      icon: activeAgent.value?.icon ?? '✦',
      title: prompt.label,
      prompt: prompt.content,
    }))
  }
  return [
    { icon: '◎', title: '规划今天', prompt: '根据我的计划，帮我安排今天最重要的三件事' },
    { icon: '✓', title: '复盘进度', prompt: '帮我复盘本周计划进度，并给出调整建议' },
    { icon: '◌', title: '查找资料', prompt: '帮我查找可靠资料，并标注信息来源' },
  ]
})

function submit(): void {
  if (!canSend.value) {
    isRecording.value = !isRecording.value
    return
  }

  emit('send', draft.value.trim())
  draft.value = ''
  isRecording.value = false
}

function usePrompt(prompt: string): void {
  draft.value = prompt
}
</script>

<template>
  <view class="chat-view">
    <scroll-view class="chat-scroll" scroll-y :scroll-with-animation="true">
      <view v-if="isFreshConversation" class="welcome-stage">
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
      </view>

      <scroll-view
        v-if="isFreshConversation && !activeAgent"
        class="agent-shortcut-scroll"
        scroll-x
        :show-scrollbar="false"
      >
        <view class="agent-shortcut-row">
          <button
            v-for="agent in agentShortcuts"
            :key="agent.code"
            class="agent-shortcut"
            :class="{
              current: agent.code === activeAgentCode,
              coming: agent.availability === 'COMING_SOON',
            }"
            @click="emit('selectAgent', agent.code)"
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

      <view v-if="!isFreshConversation" class="conversation-date">今天</view>

      <view class="chat-thread" :class="{ fresh: isFreshConversation }">
        <template v-for="item in items" :key="item.key">
          <view
            v-if="item.kind === 'message' && (!isFreshConversation || item.role === 'user')"
            class="message-row"
            :class="{ user: item.role === 'user' }"
          >
            <view v-if="item.role === 'assistant'" class="bubble-avatar">✦</view>
            <view class="bubble" :class="item.role">
              <SafeMarkdown v-if="item.role === 'assistant'" :content="item.content" />
              <text v-else>{{ item.content }}</text>
            </view>
          </view>

          <view v-else-if="item.kind === 'selection'" class="message-card">
            <text class="card-label">{{
              item.allowFreeText ? '告诉我你的安排' : '需要你的选择'
            }}</text>
            <text class="card-title">{{ item.title }}</text>
            <text class="card-description">{{ item.description }}</text>
            <text v-if="item.allowFreeText && !item.submitted" class="natural-input-hint"
              >可直接在下方输入，也可以选择常用安排</text
            >
            <view class="option-grid">
              <button
                v-for="(option, index) in item.options"
                :key="option"
                class="option-button"
                :class="{ active: item.selected === index }"
                :disabled="item.submitted"
                @click="emit('selectOption', item.key, index)"
              >
                {{ option }}
              </button>
            </view>
            <view class="card-actions">
              <text v-if="item.submitted" class="submitted-label">✓ 已提交</text>
              <button
                v-else
                class="small-button primary"
                :disabled="item.selected < 0"
                @click="emit('submitSelection', item.key)"
              >
                {{ item.submitLabel }}
              </button>
            </view>
          </view>

          <view v-else-if="item.kind === 'plan'" class="message-card plan-card">
            <view class="plan-heading">
              <view>
                <text class="card-label">计划调整</text>
                <text class="card-title">{{ item.title }}</text>
              </view>
              <text class="approval-pill" :class="item.status">
                {{
                  item.status === 'approved'
                    ? '已确认'
                    : item.status === 'editing'
                      ? '修改中'
                      : '待确认'
                }}
              </text>
            </view>
            <view class="plan-block">
              <view class="plan-block-head"
                ><text>公务员备考</text
                ><text>每周 {{ Math.round(item.weeklyMinutes / 6) / 10 }} 小时</text></view
              >
              <text class="plan-note">{{ item.description }}</text>
            </view>
            <view v-if="item.status === 'pending'" class="card-actions two">
              <button class="small-button secondary" @click="emit('editPlan', item)">
                继续修改
              </button>
              <button class="small-button primary" @click="emit('approvePlan', item)">
                确认调整
              </button>
            </view>
            <text v-else-if="item.status === 'approved'" class="plan-result"
              >✓ 调整已确认，任务正在更新</text
            >
            <text v-else class="plan-result editing">请在输入框中说明希望修改的内容</text>
          </view>

          <view v-else-if="item.kind === 'source'" class="message-card source-card">
            <text class="card-label">引用 {{ item.sources.length }} 篇资料</text>
            <text class="card-title">{{ item.title }}</text>
            <text v-if="!item.interactive" class="card-description"
              >该卡片仅展示安全文本，不能打开来源。</text
            >
            <view v-else class="source-list">
              <button
                v-for="source in item.sources"
                :key="source.citationId"
                class="source-row"
                :disabled="!source.citationId"
                @click="emit('openSource', source.citationId)"
              >
                <text class="source-index">{{ source.index }}</text>
                <view>
                  <text class="source-title">{{ source.title }}</text>
                  <text class="source-meta"
                    >{{
                      source.sourceType === 'PRIVATE_FILE'
                        ? '我的资料'
                        : source.sourceLevel === 'OFFICIAL'
                          ? '官方来源'
                          : '网页'
                    }}
                    · {{ source.domain }}</text
                  >
                </view>
                <text class="source-arrow">›</text>
              </button>
            </view>
          </view>

          <view v-else-if="item.kind === 'status'" class="message-card status-card">
            <view class="status-heading">
              <view class="spinner" />
              <view
                ><text class="card-title">{{ item.title }}</text
                ><text class="card-description">{{ item.description }}</text></view
              >
            </view>
          </view>
        </template>
      </view>
    </scroll-view>

    <view class="composer-area">
      <view class="quick-prompts">
        <button
          v-for="prompt in quickPrompts"
          :key="prompt.title"
          @click="usePrompt(prompt.prompt)"
        >
          <text class="prompt-icon">{{ prompt.icon }}</text>
          <text>{{ prompt.title }}</text>
        </button>
      </view>
      <view class="composer-shell">
        <button
          class="voice-button"
          :class="{ recording: isRecording }"
          aria-label="语音输入"
          @click="isRecording = !isRecording"
        >
          {{ isRecording ? '■' : '◖))' }}
        </button>
        <view class="input-wrap" :class="{ recording: isRecording }">
          <view v-if="attachments.length" class="attachment-list">
            <view v-for="attachment in attachments" :key="attachment.id" class="attachment-chip">
              <text>文</text><text class="attachment-name">{{ attachment.name }}</text>
              <button @click="emit('removeAttachment', attachment.id)">×</button>
            </view>
          </view>
          <input
            v-model="draft"
            :placeholder="composerPlaceholder"
            confirm-type="send"
            @confirm="submit"
          />
        </view>
        <button class="attach-button" aria-label="添加资料" @click="emit('openAttachments')">
          ＋
        </button>
        <button v-if="canSend" class="composer-action" aria-label="发送" @click="submit">↑</button>
      </view>
      <text class="ai-note">内容由 AI 生成，请核对重要信息</text>
    </view>
  </view>
</template>

<style scoped>
.chat-view {
  position: relative;
  min-height: calc(100vh - 132rpx - env(safe-area-inset-top));
}

.chat-scroll {
  height: calc(100vh - 356rpx - env(safe-area-inset-top) - env(safe-area-inset-bottom));
}

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

.quick-prompts {
  display: grid;
  width: 100%;
  grid-template-columns: repeat(3, 1fr);
  gap: 14rpx;
  margin-bottom: 12rpx;
}

.quick-prompts button {
  display: flex;
  min-height: 88rpx;
  align-items: center;
  justify-content: center;
  gap: 10rpx;
  margin: 0;
  padding: 15rpx 8rpx;
  color: #56516d;
  font-size: 19rpx;
  line-height: 1.2;
  background: rgba(255, 255, 255, 0.74);
  border: 1px solid rgba(119, 101, 239, 0.11);
  border-radius: 25rpx;
  box-shadow: 0 10rpx 28rpx rgba(66, 54, 112, 0.05);
  flex-direction: column;
}

.prompt-icon {
  color: #6c5ce7;
  font-size: 28rpx;
}

.conversation-date {
  padding: 14rpx 0 28rpx;
  color: #aaa6b7;
  font-size: 18rpx;
  text-align: center;
}

.chat-thread {
  display: flex;
  flex-direction: column;
  gap: 24rpx;
  padding: 0 2rpx 50rpx;
}

.chat-thread.fresh {
  display: none;
}

.message-row {
  display: flex;
  align-items: flex-start;
  gap: 14rpx;
}

.message-row.user {
  justify-content: flex-end;
}

.bubble-avatar {
  display: flex;
  flex: 0 0 auto;
  width: 58rpx;
  height: 58rpx;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 24rpx;
  background: linear-gradient(145deg, #6d5be5, #a095f8);
  border-radius: 20rpx;
  box-shadow: 0 8rpx 24rpx rgba(91, 72, 205, 0.2);
}

.bubble {
  max-width: 78%;
  padding: 22rpx 25rpx;
  color: #312e43;
  font-size: 23rpx;
  line-height: 1.65;
  background: rgba(255, 255, 255, 0.9);
  border: 1px solid rgba(93, 78, 160, 0.1);
  border-radius: 9rpx 28rpx 28rpx 28rpx;
  box-shadow: 0 10rpx 28rpx rgba(52, 42, 96, 0.05);
}

.bubble.user {
  color: #fff;
  background: linear-gradient(135deg, #6454e8, #7968f3);
  border: 0;
  border-radius: 28rpx 9rpx 28rpx 28rpx;
}

.message-card {
  margin-left: 72rpx;
  padding: 26rpx;
  background: rgba(255, 255, 255, 0.94);
  border: 1px solid rgba(96, 78, 177, 0.12);
  border-radius: 30rpx;
  box-shadow: 0 14rpx 38rpx rgba(48, 39, 93, 0.07);
}

.card-label {
  display: block;
  margin-bottom: 14rpx;
  color: #6556e8;
  font-size: 18rpx;
  font-weight: 750;
}

.card-title {
  display: block;
  color: #2f2b40;
  font-size: 26rpx;
  font-weight: 730;
}

.card-description {
  display: block;
  margin-top: 10rpx;
  color: #7f7a91;
  font-size: 20rpx;
  line-height: 1.55;
}

.natural-input-hint {
  display: block;
  margin-top: 14rpx;
  padding: 14rpx 16rpx;
  color: #6556e8;
  font-size: 18rpx;
  background: #f2efff;
  border-radius: 16rpx;
}

.option-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12rpx;
  margin-top: 22rpx;
}

.option-button {
  min-height: 72rpx;
  margin: 0;
  padding: 0 14rpx;
  color: #4f4a63;
  font-size: 20rpx;
  line-height: 1.35;
  background: #f7f6fc;
  border: 1px solid #e5e1f2;
  border-radius: 20rpx;
}

.option-button.active {
  color: #6556e8;
  font-weight: 700;
  background: #eeebff;
  border-color: #b7abff;
}

.card-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 22rpx;
}

.card-actions.two {
  gap: 12rpx;
}

.small-button {
  min-height: 68rpx;
  margin: 0;
  padding: 0 22rpx;
  font-size: 20rpx;
  font-weight: 700;
  line-height: 68rpx;
  border: 0;
  border-radius: 19rpx;
}

.small-button.primary {
  color: #fff;
  background: #6556e8;
}
.small-button.primary[disabled] {
  color: #aaa5bd;
  background: #e8e5ef;
}
.small-button.secondary {
  color: #413d51;
  background: #f0eef6;
}
.submitted-label,
.plan-result {
  color: #258768;
  font-size: 20rpx;
  font-weight: 650;
}
.plan-heading,
.plan-heading > view {
  display: flex;
}
.plan-heading {
  align-items: flex-start;
  justify-content: space-between;
  gap: 14rpx;
}
.plan-heading > view {
  min-width: 0;
  flex: 1;
  flex-direction: column;
}
.approval-pill {
  flex: 0 0 auto;
  padding: 8rpx 13rpx;
  color: #98611f;
  font-size: 18rpx;
  background: #fff1d9;
  border-radius: 999rpx;
}
.approval-pill.approved {
  color: #168764;
  background: #e8f7f1;
}
.plan-block {
  margin-top: 20rpx;
  padding: 20rpx;
  background: #f7f6fb;
  border-radius: 22rpx;
}
.plan-block-head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 15rpx;
  font-size: 21rpx;
  font-weight: 700;
}
.plan-note {
  display: block;
  padding-top: 15rpx;
  color: #747083;
  font-size: 19rpx;
  line-height: 1.55;
  border-top: 1px solid #e6e2ed;
}
.plan-result {
  display: block;
  margin-top: 20rpx;
  text-align: right;
}
.plan-result.editing {
  color: #a06a26;
}
.source-list {
  display: flex;
  flex-direction: column;
  gap: 12rpx;
  margin-top: 18rpx;
}
.source-row {
  display: grid;
  width: 100%;
  min-height: auto;
  grid-template-columns: 48rpx minmax(0, 1fr) auto;
  align-items: center;
  gap: 12rpx;
  margin: 0;
  padding: 17rpx;
  line-height: 1.3;
  text-align: left;
  background: #f6f5fb;
  border: 0;
  border-radius: 22rpx;
}
.source-index {
  width: 42rpx;
  height: 42rpx;
  color: #6556e8;
  font-size: 19rpx;
  font-weight: 750;
  line-height: 42rpx;
  text-align: center;
  background: #ebe8ff;
  border-radius: 14rpx;
}
.source-row > view {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4rpx;
}
.source-title {
  overflow: hidden;
  font-size: 19rpx;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-meta {
  color: #7d788f;
  font-size: 17rpx;
}
.source-arrow {
  color: #9792a6;
  font-size: 30rpx;
}
.status-heading {
  display: flex;
  gap: 18rpx;
}
.status-heading > view:last-child {
  display: flex;
  flex: 1;
  flex-direction: column;
}
.spinner {
  width: 40rpx;
  height: 40rpx;
  flex: 0 0 auto;
  border: 5rpx solid #ddd8ff;
  border-top-color: #6556e8;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.composer-area {
  position: fixed;
  z-index: 20;
  right: 0;
  bottom: 0;
  left: 0;
  box-sizing: border-box;
  padding: 10rpx 24rpx calc(13rpx + env(safe-area-inset-bottom));
  background: linear-gradient(180deg, rgba(243, 241, 255, 0), #f3f1ff 20%);
}

.agent-shortcut-scroll {
  width: 100%;
  white-space: nowrap;
}

.welcome-stage .agent-shortcut-scroll {
  margin-top: 52rpx;
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

.composer-shell {
  display: flex;
  min-height: 92rpx;
  align-items: flex-end;
  gap: 8rpx;
  padding: 10rpx;
  background: rgba(255, 255, 255, 0.97);
  border: 1px solid rgba(88, 70, 163, 0.13);
  border-radius: 32rpx;
  box-shadow: 0 18rpx 50rpx rgba(55, 43, 105, 0.15);
}

.voice-button,
.attach-button,
.composer-action {
  flex: 0 0 auto;
  width: 70rpx;
  height: 70rpx;
  margin: 0;
  padding: 0;
  color: #4d485f;
  font-size: 24rpx;
  line-height: 70rpx;
  background: transparent;
  border: 0;
  border-radius: 22rpx;
}

.voice-button.recording {
  color: #fff;
  background: #e55763;
}
.attach-button {
  font-size: 38rpx;
}
.composer-action {
  color: #fff;
  font-size: 32rpx;
  background: #6556e8;
}

.input-wrap {
  display: flex;
  min-width: 0;
  min-height: 70rpx;
  flex: 1;
  justify-content: center;
  flex-direction: column;
}

.input-wrap input {
  width: 100%;
  height: 70rpx;
  color: #292638;
  font-size: 23rpx;
}
.attachment-list {
  display: flex;
  gap: 8rpx;
  padding: 5rpx 0;
  flex-wrap: wrap;
}
.attachment-chip {
  display: flex;
  max-width: 300rpx;
  align-items: center;
  gap: 7rpx;
  padding: 6rpx 10rpx;
  color: #5b4fc5;
  font-size: 17rpx;
  background: #efecff;
  border-radius: 12rpx;
}
.attachment-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attachment-chip button {
  width: 30rpx;
  height: 30rpx;
  margin: 0;
  padding: 0;
  color: #7b75a1;
  font-size: 22rpx;
  line-height: 28rpx;
  background: transparent;
  border: 0;
}
.ai-note {
  display: block;
  margin-top: 9rpx;
  color: #aaa6b6;
  font-size: 15rpx;
  text-align: center;
}

button::after {
  border: 0;
}
</style>
