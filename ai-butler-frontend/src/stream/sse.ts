export interface SSEEvent {
  event: string
  data: string
  id?: string
}

interface PendingEvent {
  event: string
  data: string[]
  id?: string
}

function emptyEvent(): PendingEvent {
  return { event: 'message', data: [] }
}

export class SSEParser {
  private readonly decoder = new TextDecoder('utf-8', { fatal: true })
  private line = ''
  private pendingCarriageReturn = false
  private pendingEvent = emptyEvent()

  push(chunk: Uint8Array): SSEEvent[] {
    return this.processText(this.decoder.decode(chunk, { stream: true }))
  }

  finish(): SSEEvent[] {
    const events = this.processText(this.decoder.decode())
    if (this.pendingCarriageReturn) {
      this.pendingCarriageReturn = false
      this.processLine(events)
    }
    if (this.line) {
      this.processLine(events)
    }
    this.dispatch(events)
    return events
  }

  private processText(text: string): SSEEvent[] {
    const events: SSEEvent[] = []
    for (const character of text) {
      if (this.pendingCarriageReturn) {
        this.pendingCarriageReturn = false
        this.processLine(events)
        if (character === '\n') {
          continue
        }
      }
      if (character === '\r') {
        this.pendingCarriageReturn = true
      } else if (character === '\n') {
        this.processLine(events)
      } else {
        this.line += character
      }
    }
    return events
  }

  private processLine(events: SSEEvent[]): void {
    const line = this.line
    this.line = ''
    if (!line) {
      this.dispatch(events)
      return
    }
    if (line.startsWith(':')) {
      return
    }
    const separator = line.indexOf(':')
    const field = separator === -1 ? line : line.slice(0, separator)
    let value = separator === -1 ? '' : line.slice(separator + 1)
    if (value.startsWith(' ')) {
      value = value.slice(1)
    }
    if (field === 'data') {
      this.pendingEvent.data.push(value)
    } else if (field === 'event') {
      this.pendingEvent.event = value || 'message'
    } else if (field === 'id' && !value.includes('\0')) {
      this.pendingEvent.id = value
    }
  }

  private dispatch(events: SSEEvent[]): void {
    if (this.pendingEvent.data.length === 0) {
      this.pendingEvent = emptyEvent()
      return
    }
    events.push({
      event: this.pendingEvent.event,
      data: this.pendingEvent.data.join('\n'),
      ...(this.pendingEvent.id === undefined ? {} : { id: this.pendingEvent.id }),
    })
    this.pendingEvent = emptyEvent()
  }
}

export interface SequencedRunEvent {
  runId: string
  sequence: number
}

export class SequenceDeduplicator {
  private readonly lastSequence = new Map<string, number>()

  accept(event: SequencedRunEvent): boolean {
    const previous = this.lastSequence.get(event.runId) ?? 0
    if (event.sequence <= previous) {
      return false
    }
    this.lastSequence.set(event.runId, event.sequence)
    return true
  }

  cursor(runId: string): number {
    return this.lastSequence.get(runId) ?? 0
  }
}
