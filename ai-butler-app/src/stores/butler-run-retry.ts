import type { Ref } from 'vue'

import { butlerApi } from '@/api/butler'
import { asObject, numberValue, stringValue } from '@/stores/butler-projections'
import type { ChatItem, ConversationViewModel } from '@/types/view-models'

interface RunRetryState {
  conversations: Ref<ConversationViewModel[]>
  activeConversationId: Ref<string | null>
  chatItems: Ref<ChatItem[]>
  activeRunId: Ref<string | null>
}

type RefreshConversations = (accessToken: string) => Promise<void>
type StartRunStream = (
  runId: string,
  conversationId: string,
  eventsUrl: string,
  ticket: string,
  after: number,
  accessToken: string,
) => void

/**
 * 创建失败 run 的安全重试动作。
 *
 * retry 接口只恢复服务端 run，不返回新流票据；成功后必须重新申请票据，并从
 * 服务端 sequence 续传。expectedAttempt 由错误 SSE 事件提供，用于拒绝重复点击
 * 或其他客户端已经恢复后的陈旧操作。
 */
export function createRunRetry(
  state: RunRetryState,
  refreshConversations: RefreshConversations,
  startStream: StartRunStream,
): (runId: string, expectedAttempt: number | undefined, accessToken: string) => Promise<void> {
  return async (runId, expectedAttempt, accessToken) => {
    const conversationId =
      state.conversations.value.find((item) => item.runId === runId)?.key ??
      state.activeConversationId.value
    if (!conversationId) throw new Error('无法确定待重试的对话')

    let attempt = expectedAttempt
    if (attempt === undefined) {
      // 热更新前产生的错误卡没有 attempt；重新读取服务端事实后再提交，仍由
      // expected_attempt 乐观锁保护，不能用猜测值绕过并发约束。
      const run = await butlerApi.run(runId, accessToken)
      const error = asObject(run.error)
      if (stringValue(run, 'status') !== 'FAILED_RETRYABLE' || error?.retryable !== true) {
        throw new Error('当前运行已不可重试，请刷新后查看最新状态')
      }
      attempt = numberValue(run, 'attempt')
    }

    await butlerApi.retryRun(
      runId,
      {
        schema_version: '1.0',
        expected_attempt: attempt,
        execution_policy: 'REJECT',
      },
      accessToken,
    )
    const stream = await butlerApi.streamTicket(runId, accessToken)
    const statusIndex = state.chatItems.value.findIndex((item) => item.key === `progress-${runId}`)
    if (state.activeConversationId.value === conversationId && statusIndex >= 0) {
      state.chatItems.value[statusIndex] = {
        key: `progress-${runId}`,
        kind: 'status',
        state: 'loading',
        title: '正在重新生成',
        description: '处理完成后会自动显示完整回答。',
        runId,
      }
    }
    state.activeRunId.value = runId
    await refreshConversations(accessToken)
    startStream(
      runId,
      conversationId,
      stringValue(stream, 'events_url'),
      stringValue(stream, 'ticket'),
      numberValue(stream, 'last_sequence'),
      accessToken,
    )
  }
}
