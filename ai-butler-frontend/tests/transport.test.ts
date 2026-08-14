import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { connectRunStream } from '@/stream/transport'

class EventSourceMock {
  static last: EventSourceMock
  listeners = new Map<string, (event: { data: string }) => void>()
  onerror: (() => void) | null = null
  close = vi.fn()

  constructor(readonly url: string) {
    EventSourceMock.last = this
  }

  addEventListener(name: string, listener: EventListener): void {
    this.listeners.set(name, listener as unknown as (event: { data: string }) => void)
  }
}

const envelope = (sequence: number) =>
  JSON.stringify({ run_id: 'run-1', sequence, attempt: 1, payload: { delta: '好' } })

describe('run stream platform adapters', () => {
  beforeEach(() => vi.stubGlobal('EventSource', EventSourceMock))
  afterEach(() => vi.unstubAllGlobals())

  it('uses EventSource, drops duplicate sequences and closes cleanly', async () => {
    const onEvent = vi.fn()
    const onError = vi.fn()
    const connection = connectRunStream({
      runId: 'run-1',
      eventsUrl: '/events',
      ticket: 'secret ticket',
      after: 0,
      onEvent,
      onError,
    })
    const listener = EventSourceMock.last.listeners.get('message.delta')!
    listener({ data: envelope(1) })
    listener({ data: envelope(1) })
    expect(onEvent).toHaveBeenCalledTimes(1)
    EventSourceMock.last.listeners.get('error')!({ data: '{}' })
    expect(onError).toHaveBeenCalled()
    EventSourceMock.last.onerror?.()
    connection.close()
    expect(EventSourceMock.last.close).toHaveBeenCalled()
  })

  it('parses mini-program UTF-8 chunks and aborts malformed frames', () => {
    vi.stubGlobal('EventSource', undefined)
    const callbacks: Array<(event: { data: ArrayBuffer }) => void> = []
    const task = {
      abort: vi.fn(),
      onChunkReceived: (callback: (event: { data: ArrayBuffer }) => void) =>
        callbacks.push(callback),
    }
    vi.stubGlobal('uni', { request: vi.fn(() => task) })
    const onEvent = vi.fn()
    const onError = vi.fn()
    const connection = connectRunStream({
      runId: 'run-1',
      eventsUrl: '/events',
      ticket: 'ticket',
      after: 0,
      onEvent,
      onError,
    })
    const encoded = new TextEncoder().encode(`event: message.delta\ndata: ${envelope(2)}\n\n`)
    callbacks[0]!({ data: encoded.buffer as ArrayBuffer })
    expect(onEvent).toHaveBeenCalledTimes(1)

    const malformed = new TextEncoder().encode('event: message.delta\ndata: nope\n\n')
    callbacks[0]!({ data: malformed.buffer as ArrayBuffer })
    expect(onError).toHaveBeenCalled()
    expect(task.abort).toHaveBeenCalled()
    connection.close()
  })
})
