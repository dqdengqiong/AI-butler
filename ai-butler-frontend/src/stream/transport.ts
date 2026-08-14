import { appConfig } from '@/config'
import { SequenceDeduplicator, SSEParser } from '@/stream/sse'

export interface RunStreamEvent {
  event: string
  runId: string
  sequence: number
  attempt: number
  payload: Record<string, unknown>
}

interface StreamOptions {
  runId: string
  eventsUrl: string
  ticket: string
  after: number
  onEvent: (event: RunStreamEvent) => void | Promise<void>
  onError: () => void | Promise<void>
}

interface ChunkedRequestTask {
  abort(): void
  onChunkReceived(callback: (event: { data: ArrayBuffer }) => void): void
}

function parseEnvelope(event: string, data: string): RunStreamEvent {
  const decoded: unknown = JSON.parse(data)
  if (typeof decoded !== 'object' || decoded === null) throw new Error('invalid SSE envelope')
  const envelope = decoded as Record<string, unknown>
  if (
    typeof envelope.run_id !== 'string' ||
    typeof envelope.sequence !== 'number' ||
    typeof envelope.attempt !== 'number' ||
    typeof envelope.payload !== 'object' ||
    envelope.payload === null
  ) {
    throw new Error('invalid SSE envelope')
  }
  return {
    event,
    runId: envelope.run_id,
    sequence: envelope.sequence,
    attempt: envelope.attempt,
    payload: envelope.payload as Record<string, unknown>,
  }
}

/**
 * 创建平台专属 SSE 连接，但向 Store 暴露完全相同的事件模型。
 * H5 使用 EventSource；小程序按字节流解析 UTF-8 和半帧，页面关闭仅关闭连接，
 * 不调用取消 run。
 */
export function connectRunStream(options: StreamOptions): { close(): void } {
  const deduplicator = new SequenceDeduplicator()
  if (options.after > 0) {
    deduplicator.accept({ runId: options.runId, sequence: options.after })
  }
  const url = `${appConfig.apiBaseUrl}${options.eventsUrl}?ticket=${encodeURIComponent(options.ticket)}&after=${options.after}`

  if (typeof EventSource !== 'undefined') {
    const source = new EventSource(url)
    const names = [
      'run.accepted',
      'run.status',
      'progress',
      'message.start',
      'message.delta',
      'message.reset',
      'message.completed',
      'interrupt',
      'run.completed',
      'run.cancelled',
      'error',
    ]
    for (const name of names) {
      source.addEventListener(name, (raw) => {
        try {
          const event = parseEnvelope(name, (raw as MessageEvent<string>).data)
          if (deduplicator.accept({ runId: event.runId, sequence: event.sequence })) {
            void options.onEvent(event)
          }
        } catch {
          void options.onError()
        }
      })
    }
    source.onerror = () => void options.onError()
    return { close: () => source.close() }
  }

  const parser = new SSEParser()
  const task = uni.request({
    url,
    method: 'GET',
    enableChunked: true,
    success: () => undefined,
    fail: () => void options.onError(),
  } as unknown as UniApp.RequestOptions) as unknown as ChunkedRequestTask
  task.onChunkReceived((chunk) => {
    try {
      const events = parser.push(new Uint8Array(chunk.data))
      for (const raw of events) {
        const event = parseEnvelope(raw.event, raw.data)
        if (deduplicator.accept({ runId: event.runId, sequence: event.sequence })) {
          void options.onEvent(event)
        }
      }
    } catch {
      task.abort()
      void options.onError()
    }
  })
  return { close: () => task.abort() }
}
