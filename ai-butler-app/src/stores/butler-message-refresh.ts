import type { Ref } from 'vue'

import { butlerApi, type ApiObject, type MessageResponse } from '@/api/butler'
import {
  asObject,
  mapMessage,
  numberValue,
  runErrorPresentation,
  stringValue,
} from '@/stores/butler-projections'
import type { ChatItem, ConversationViewModel } from '@/types/view-models'

interface MessageRefreshState {
  activeConversationId: Ref<string | null>
  conversations: Ref<ConversationViewModel[]>
  chatItems: Ref<ChatItem[]>
}

/**
 * 刷新会话消息，并为刷新后仍可重试的 run 恢复错误卡。
 *
 * 错误 SSE 是瞬时投影，页面重载后只能从 run API 重新取得 attempt。只有服务端
 * 同时确认 FAILED_RETRYABLE 和 error.retryable 时才创建操作卡，避免把终态失败
 * 误展示成可恢复状态。
 */
export async function refreshConversationMessages(
  accessToken: string,
  conversationId: string | null,
  state: MessageRefreshState,
): Promise<void> {
  if (!conversationId) {
    state.chatItems.value = []
    return
  }
  const conversation = state.conversations.value.find((item) => item.key === conversationId)
  const retryRunId = conversation?.runStatus === 'FAILED_RETRYABLE' ? conversation.runId : undefined
  const [response, run] = await Promise.all([
    butlerApi.messages(conversationId, accessToken),
    retryRunId ? butlerApi.run(retryRunId, accessToken) : Promise.resolve(null),
  ])
  // SSE 可以在用户查看另一个历史会话时完成，旧 run 不得覆盖当前时间线。
  if (state.activeConversationId.value !== conversationId) return

  const items = response.items.flatMap((item: MessageResponse) =>
    mapMessage(item as unknown as ApiObject),
  )
  const error = run ? asObject(run.error) : null
  if (
    retryRunId &&
    run &&
    stringValue(run, 'status') === 'FAILED_RETRYABLE' &&
    error?.retryable === true
  ) {
    const copy = runErrorPresentation(stringValue(error, 'code'), true)
    items.push({
      key: `progress-${retryRunId}`,
      kind: 'status',
      state: 'error',
      ...copy,
      runId: retryRunId,
      attempt: numberValue(run, 'attempt'),
      retryable: true,
    })
  }
  state.chatItems.value = items
}
