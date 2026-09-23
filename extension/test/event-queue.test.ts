import { describe, expect, it, vi } from "vitest";
import { DurableEventQueue, type EventStore, type QueuedEvent } from "../src/background/event-queue";

class InMemoryStore implements EventStore {
  private events: QueuedEvent[] = [];
  async getAll() { return [...this.events]; }
  async add(event: QueuedEvent) { this.events.push(event); }
  async remove(eventId: string) {
    this.events = this.events.filter((e) => e.eventId !== eventId);
  }
}

function makeEvent(overrides: Partial<QueuedEvent> = {}): QueuedEvent {
  return {
    eventId: "evt_1", clientSequence: 1, handoffSessionId: "hs_1",
    eventType: "field_detected", eventPayload: {}, observedAt: "2026-08-24T00:00:00Z",
    ...overrides,
  };
}

describe("DurableEventQueue", () => {
  it("removes an event from the store only after the sender acknowledges", async () => {
    const store = new InMemoryStore();
    const sender = vi.fn().mockResolvedValue(true);
    const queue = new DurableEventQueue(store, sender);

    await queue.enqueue(makeEvent());
    expect(await store.getAll()).toHaveLength(1);

    await queue.flush();
    expect(sender).toHaveBeenCalledOnce();
    expect(await store.getAll()).toHaveLength(0);
  });

  it("keeps a failed event queued for retry", async () => {
    const store = new InMemoryStore();
    const sender = vi.fn().mockResolvedValue(false);
    const queue = new DurableEventQueue(store, sender);

    await queue.enqueue(makeEvent());
    await queue.flush();
    expect(await store.getAll()).toHaveLength(1);
  });

  it("retries in clientSequence order after simulated worker restart", async () => {
    const store = new InMemoryStore();
    await store.add(makeEvent({ eventId: "evt_2", clientSequence: 2 }));
    await store.add(makeEvent({ eventId: "evt_1", clientSequence: 1 }));

    const order: string[] = [];
    const sender = vi.fn().mockImplementation(async (event: QueuedEvent) => {
      order.push(event.eventId);
      return true;
    });
    // simulates a fresh queue instance after the service worker restarted,
    // reading whatever was already durably stored
    const queue = new DurableEventQueue(store, sender);
    await queue.flush();

    expect(order).toEqual(["evt_1", "evt_2"]);
  });

  it("never mints a new eventId to resolve a failed delivery", async () => {
    const store = new InMemoryStore();
    let attempts = 0;
    const sender = vi.fn().mockImplementation(async () => {
      attempts += 1;
      return attempts > 1; // fails first attempt, succeeds second
    });
    const queue = new DurableEventQueue(store, sender);
    await queue.enqueue(makeEvent());

    await queue.flush(); // fails
    await queue.flush(); // succeeds

    const sentEventIds = sender.mock.calls.map(([event]) => event.eventId);
    expect(new Set(sentEventIds)).toEqual(new Set(["evt_1"]));
  });

  // Guards specifically against the wrong fix for overlapping flush()
  // calls: simply returning the same already-in-flight flush Promise to
  // every concurrent caller. That would strand a newly enqueued event --
  // if flush A already took its snapshot of the queue before event B is
  // enqueued, and flush B's caller just joins A's Promise instead of
  // running its own pass, B is never delivered by either call and no
  // later trigger exists to pick it up.
  it("a flush requested while another is in flight still delivers an event enqueued in between", async () => {
    const store = new InMemoryStore();
    const firstEventReceived = deferred<void>();
    const releaseFirstSend = deferred<void>();
    const delivered: string[] = [];

    const sender = vi.fn().mockImplementation(async (event: QueuedEvent) => {
      if (event.eventId === "evt_1") {
        // Signal that flush A has started sending evt_1 (i.e. it has
        // already taken its snapshot and is mid-send), then block until
        // the test explicitly releases it -- this is the window during
        // which event B is enqueued and flush B is requested.
        firstEventReceived.resolve();
        await releaseFirstSend.promise;
      }
      delivered.push(event.eventId);
      return true;
    });

    const queue = new DurableEventQueue(store, sender);
    await queue.enqueue(makeEvent({ eventId: "evt_1", clientSequence: 1 }));

    const flushA = queue.flush();
    await firstEventReceived.promise;

    // Event B is durably enqueued WHILE flush A is still in flight and
    // has already snapshotted the queue without B in it.
    await queue.enqueue(makeEvent({ eventId: "evt_2", clientSequence: 2 }));
    const flushB = queue.flush();

    releaseFirstSend.resolve();
    await Promise.all([flushA, flushB]);

    expect(delivered).toEqual(["evt_1", "evt_2"]);
    expect(await store.getAll()).toHaveLength(0);
  });
});

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}
