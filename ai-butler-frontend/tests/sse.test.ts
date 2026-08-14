import { describe, expect, it } from 'vitest'

import { SequenceDeduplicator, SSEParser } from '@/stream/sse'

const encoder = new TextEncoder()

describe('SSEParser', () => {
  it('buffers partial frames and joins multi-line data', () => {
    const parser = new SSEParser()
    expect(parser.push(encoder.encode('id: 7\nevent: progress\ndata: first'))).toEqual([])
    expect(parser.push(encoder.encode('\ndata: second\n\n'))).toEqual([
      { id: '7', event: 'progress', data: 'first\nsecond' },
    ])
  })

  it('decodes UTF-8 characters split across byte chunks', () => {
    const parser = new SSEParser()
    const bytes = encoder.encode('data: 你好\n\n')
    const split = bytes.indexOf(0xe5) + 1
    expect(parser.push(bytes.slice(0, split))).toEqual([])
    expect(parser.push(bytes.slice(split))).toEqual([{ event: 'message', data: '你好' }])
  })

  it('handles CRLF split between chunks and ignores heartbeats', () => {
    const parser = new SSEParser()
    expect(parser.push(encoder.encode(': heartbeat\r'))).toEqual([])
    expect(parser.push(encoder.encode('\n\r'))).toEqual([])
    expect(parser.push(encoder.encode('\ndata: ok\r\n\r\n'))).toEqual([
      { event: 'message', data: 'ok' },
    ])
  })

  it('dispatches a final unterminated data event on finish', () => {
    const parser = new SSEParser()
    expect(parser.push(encoder.encode('data: final'))).toEqual([])
    expect(parser.finish()).toEqual([{ event: 'message', data: 'final' }])
  })

  it('handles a trailing carriage return on finish', () => {
    const parser = new SSEParser()
    expect(parser.push(encoder.encode('data: final\r'))).toEqual([])
    expect(parser.finish()).toEqual([{ event: 'message', data: 'final' }])
  })

  it('ignores ids containing null characters', () => {
    const parser = new SSEParser()
    expect(parser.push(encoder.encode('id: invalid\0id\ndata: ok\n\n'))).toEqual([
      { event: 'message', data: 'ok' },
    ])
  })
})

describe('SequenceDeduplicator', () => {
  it('rejects duplicate and out-of-order events per run', () => {
    const deduplicator = new SequenceDeduplicator()
    expect(deduplicator.accept({ runId: 'run-a', sequence: 1 })).toBe(true)
    expect(deduplicator.accept({ runId: 'run-a', sequence: 1 })).toBe(false)
    expect(deduplicator.accept({ runId: 'run-a', sequence: 0 })).toBe(false)
    expect(deduplicator.accept({ runId: 'run-b', sequence: 1 })).toBe(true)
    expect(deduplicator.cursor('run-a')).toBe(1)
    expect(deduplicator.cursor('missing')).toBe(0)
  })
})
