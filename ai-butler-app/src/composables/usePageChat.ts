import type { Ref } from 'vue'
import { ref } from 'vue'
import { storeToRefs } from 'pinia'

import { ApiError } from '@/api/client'
import { useButlerStore } from '@/stores/butler'
import type { ChatItem, UploadedAttachment } from '@/types/view-models'

type PlanChatItem = Extract<ChatItem, { kind: 'plan' }>

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

/** 封装聊天卡片交互、审批和发送失败恢复，页面只负责连接视图事件。 */
export function usePageChat(token: () => string, attachments: Ref<UploadedAttachment[]>) {
  const butler = useButlerStore()
  const { chatItems, stagedScene } = storeToRefs(butler)
  const editingPlan = ref<PlanChatItem | null>(null)
  const submittingApprovalIds = new Set<string>()

  function selectOption(itemKey: string, optionIndex: number): void {
    const item = chatItems.value.find((entry) => entry.key === itemKey)
    if (item?.kind === 'selection' && !item.submitted) item.selected = optionIndex
  }

  async function submitSelection(itemKey: string): Promise<void> {
    const item = chatItems.value.find((entry) => entry.key === itemKey)
    if (item?.kind !== 'selection' || item.submitted) return
    const optionId = item.optionIds?.[item.selected]
    if (!item.cardId || !optionId) return
    item.submitted = true
    try {
      await butler.sendMessage('', token(), { cardId: item.cardId, optionId })
    } catch (error) {
      // 服务端未接受前恢复卡片可操作状态，避免一次网络错误永久锁住当前中断。
      item.submitted = false
      uni.showToast({ title: error instanceof Error ? error.message : '提交失败', icon: 'none' })
    }
  }

  async function approvePlan(item: PlanChatItem): Promise<void> {
    if (item.status !== 'pending' || submittingApprovalIds.has(item.approvalId)) return
    submittingApprovalIds.add(item.approvalId)
    try {
      await butler.approvePlan(item, 'APPROVE', token())
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.code === 'OTHER_CONVERSATION_RUNNING' &&
        (await confirmSwitch('另一项任务正在处理', '停止当前处理并确认这份计划吗？', '停止并确认'))
      ) {
        await butler.approvePlan(item, 'APPROVE', token(), undefined, 'CANCEL_OTHER')
        return
      }
      uni.showToast({ title: error instanceof Error ? error.message : '审批失败', icon: 'none' })
    } finally {
      submittingApprovalIds.delete(item.approvalId)
    }
  }

  function editPlan(item: PlanChatItem): void {
    if (item.status !== 'pending') return
    item.status = 'editing'
    editingPlan.value = item
    uni.showToast({ title: '请在输入框继续说明', icon: 'none' })
  }

  async function sendMessage(content: string): Promise<void> {
    const normalized = content.trim()
    const clientMessageId = `message-${Date.now()}-${Math.random().toString(36).slice(2)}`
    try {
      if (editingPlan.value) {
        await butler.approvePlan(editingPlan.value, 'EDIT', token(), normalized)
        editingPlan.value = null
      } else {
        const wasStagedWelcome = stagedScene.value !== null
        const response = await butler.sendMessage(
          normalized || '请处理我添加的资料',
          token(),
          undefined,
          attachments.value.map((item) => item.id),
          { clientMessageId },
        )
        if (response.transition.kind === 'CREATED' && !wasStagedWelcome) {
          uni.showToast({ title: '已为你整理为新话题', icon: 'none' })
        }
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
        const response = await butler.sendMessage(
          normalized || '请处理我添加的资料',
          token(),
          undefined,
          attachments.value.map((item) => item.id),
          { clientMessageId, contextPolicy: 'ARCHIVE_AND_START', executionPolicy: 'CANCEL_OTHER' },
        )
        attachments.value = []
        if (response.transition.kind === 'CREATED') {
          uni.showToast({ title: '已为你整理为新话题', icon: 'none' })
        }
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
          undefined,
          attachments.value.map((item) => item.id),
          { clientMessageId, executionPolicy: 'CANCEL_OTHER' },
        )
        attachments.value = []
        return
      }
      uni.showToast({ title: error instanceof Error ? error.message : '发送失败', icon: 'none' })
    }
  }

  return { approvePlan, editPlan, editingPlan, selectOption, sendMessage, submitSelection }
}
