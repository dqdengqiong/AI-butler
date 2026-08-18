import type { Ref } from 'vue'
import { storeToRefs } from 'pinia'

import { ApiError } from '@/api/client'
import { useButlerStore } from '@/stores/butler'
import type { ChatItem, UploadedAttachment } from '@/types/view-models'

type PreviewItem = Extract<ChatItem, { kind: 'planPreview' }>
type StatusChatItem = Extract<ChatItem, { kind: 'status' }>

function confirmSwitch(title: string, content: string, confirmText: string): Promise<boolean> {
  return new Promise((resolve) => {
    uni.showModal({
      title,
      content,
      confirmText,
      cancelText: '继续当前话题',
      success: (result) => resolve(result.confirm),
      fail: () => resolve(false),
    })
  })
}

/** 聊天交互只提交自然语言；计划预览也由普通消息触发。 */
export function usePageChat(token: () => string, attachments: Ref<UploadedAttachment[]>) {
  const butler = useButlerStore()
  const { stagedScene } = storeToRefs(butler)
  async function confirmPlan(item: PreviewItem): Promise<void> {
    try {
      await butler.confirmPlanPreview(item, token())
      uni.showToast({ title: '正式计划已创建', icon: 'success' })
    } catch (error) {
      uni.showToast({ title: error instanceof Error ? error.message : '确认失败', icon: 'none' })
    }
  }

  async function retryRun(item: StatusChatItem): Promise<void> {
    if (item.retryable === false || !item.runId || item.retrying) return
    item.retrying = true
    try {
      await butler.retryRun(item.runId, item.attempt, token())
    } catch (error) {
      item.retrying = false
      uni.showToast({ title: error instanceof Error ? error.message : '重试失败', icon: 'none' })
    }
  }

  async function sendMessage(content: string): Promise<void> {
    const normalized = content.trim()
    const clientMessageId = `message-${Date.now()}-${Math.random().toString(36).slice(2)}`
    try {
      const wasStagedWelcome = stagedScene.value !== null
      const response = await butler.sendMessage(
        normalized || '请处理我添加的资料',
        token(),
        attachments.value.map((item) => item.id),
        { clientMessageId },
      )
      if (response.transition.kind === 'CREATED' && !wasStagedWelcome) {
        uni.showToast({ title: '已为你整理为新话题', icon: 'none' })
      }
      attachments.value = []
    } catch (error) {
      if (error instanceof ApiError && error.code === 'TOPIC_SWITCH_CONFIRMATION_REQUIRED') {
        const confirmed = await confirmSwitch(
          '开始新话题？',
          '我可以暂存当前话题，并从这条消息开始整理为新话题。',
          '开始新话题',
        )
        if (!confirmed) return
        await butler.sendMessage(
          normalized || '请处理我添加的资料',
          token(),
          attachments.value.map((item) => item.id),
          { clientMessageId, contextPolicy: 'ARCHIVE_AND_START', executionPolicy: 'CANCEL_OTHER' },
        )
        attachments.value = []
        return
      }
      if (error instanceof ApiError && error.code === 'OTHER_CONVERSATION_RUNNING') {
        const confirmed = await confirmSwitch(
          '另一项任务正在处理',
          '是否停止当前处理并切换到这里？',
          '停止并切换',
        )
        if (!confirmed) return
        await butler.sendMessage(
          normalized || '请处理我添加的资料',
          token(),
          attachments.value.map((item) => item.id),
          { clientMessageId, executionPolicy: 'CANCEL_OTHER' },
        )
        attachments.value = []
        return
      }
      uni.showToast({ title: error instanceof Error ? error.message : '发送失败', icon: 'none' })
    }
  }

  return {
    confirmPlan,
    retryRun,
    sendMessage,
  }
}
