export interface QueuedEvent {
  eventId: string;
  clientSequence: number;
  handoffSessionId: string;
  eventType: string;
  eventPayload: Record<string, unknown>;
  normalizedFieldType?: string;
  pageFieldKey?: string;
  observedAt: string;
}

export interface EventStore {
  getAll(): Promise<QueuedEvent[]>;
  add(event: QueuedEvent): Promise<void>;
  remove(eventId: string): Promise<void>;
}

export class DurableEventQueue {
  // Chains each requested flush behind the previous one, WITHOUT letting
  // concurrent callers collapse onto a single shared in-flight Promise.
  // Sharing one in-flight flush Promise is the wrong fix: if flush A has
  // already taken its snapshot of the queue when event B is enqueued, and
  // flush B's caller simply awaits A's already-running Promise instead of
  // running its own pass, B is silently never delivered (A's snapshot
  // never contained it, and no later trigger exists to pick it up). Each
  // call to flush() here always performs its own read-send-remove pass
  // once it's its turn; only the ordering (not the work) is serialized.
  private flushChain: Promise<void> = Promise.resolve();

  constructor(
    private readonly store: EventStore,
    private readonly sender: (event: QueuedEvent) => Promise<boolean>,
  ) {}

  async enqueue(event: QueuedEvent): Promise<void> {
    // Persisted before any network attempt (design spec Section 13) —
    // this is what survives a killed service worker.
    await this.store.add(event);
  }

  async flush(): Promise<void> {
    const runThisFlush = async (): Promise<void> => {
      const pending = await this.store.getAll();
      const inOrder = [...pending].sort((a, b) => a.clientSequence - b.clientSequence);
      for (const event of inOrder) {
        const acknowledged = await this.sender(event);
        if (acknowledged) {
          await this.store.remove(event.eventId);
        }
        // On failure the event simply stays in the store with its original
        // eventId; the caller never mints a new one to "resolve" the retry.
      }
    };
    const scheduled = this.flushChain.then(runThisFlush);
    // A failed flush pass must not permanently jam every flush queued
    // after it -- swallow for chaining purposes only; `scheduled` itself
    // (returned below) still rejects so this call's own caller sees it.
    this.flushChain = scheduled.catch(() => {});
    return scheduled;
  }
}
