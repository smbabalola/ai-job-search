import type { EventStore, QueuedEvent } from "./event-queue";

const STORAGE_KEY = "handoff_event_queue";

// add() and remove() each need to read the current array, transform it,
// and write it back -- a read-modify-write. Two of these overlapping
// (e.g. two field_detected/value_inserted messages routed back-to-back
// with no await between the sendMessage() calls that triggered them)
// causes a lost update: the second write's "current array" snapshot was
// taken before the first write landed, so the first write's change is
// silently discarded when the second write commits. This queue is a
// per-instance serialization: every mutation (add or remove) is chained
// onto the same Promise, so the actual read-transform-write for mutation
// N+1 never begins until mutation N's write has fully committed.
//
// getAll() is deliberately NOT chained onto this same queue -- a public
// getAll() awaiting the mutation chain while a mutation's own
// read-transform-write body also calls getAll() internally would await
// itself and deadlock. Instead each mutation calls an internal,
// unserialized read directly; ordinary external callers reading via the
// public getAll() are unaffected by this because they never sit inside
// the chain themselves.
export class ChromeEventStore implements EventStore {
  private mutationChain: Promise<void> = Promise.resolve();

  private async readAll(): Promise<QueuedEvent[]> {
    const result = await chrome.storage.local.get(STORAGE_KEY);
    return (result[STORAGE_KEY] as QueuedEvent[] | undefined) ?? [];
  }

  async getAll(): Promise<QueuedEvent[]> {
    return this.readAll();
  }

  private mutate(transform: (current: QueuedEvent[]) => QueuedEvent[]): Promise<void> {
    const next = this.mutationChain.then(async () => {
      const current = await this.readAll();
      await chrome.storage.local.set({ [STORAGE_KEY]: transform(current) });
    });
    // A failed mutation must not permanently jam the chain for every
    // mutation queued after it -- swallow the rejection for chaining
    // purposes only; `next` itself (returned below) still rejects so the
    // caller sees the failure.
    this.mutationChain = next.catch(() => {});
    return next;
  }

  async add(event: QueuedEvent): Promise<void> {
    await this.mutate((current) => [...current, event]);
  }

  async remove(eventId: string): Promise<void> {
    await this.mutate((current) => current.filter((existing) => existing.eventId !== eventId));
  }
}
