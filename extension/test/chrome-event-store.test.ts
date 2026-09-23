import { beforeEach, describe, expect, it } from "vitest";
import { ChromeEventStore } from "../src/background/chrome-event-store";
import type { QueuedEvent } from "../src/background/event-queue";

// A deterministic fake chrome.storage.local whose get()/set() can be
// gated behind manually-resolved barriers, so a test can force two
// read-modify-write operations to genuinely overlap (one's read
// happening before the other's write lands) instead of happening to run
// sequentially just because nothing in a real chrome.storage.local call
// actually yields long enough to interleave in a unit test process.
class BarrieredFakeStorage {
  private data: Record<string, unknown> = {};
  // When set, get()/set() await this instead of resolving immediately --
  // lets a test hold multiple concurrent operations open at once and
  // release them in a controlled order.
  gate: Promise<void> | null = null;

  async get(key: string): Promise<Record<string, unknown>> {
    if (this.gate) await this.gate;
    return { [key]: this.data[key] };
  }

  async set(items: Record<string, unknown>): Promise<void> {
    if (this.gate) await this.gate;
    Object.assign(this.data, items);
  }
}

function installFakeChromeStorage(storage: BarrieredFakeStorage): void {
  (globalThis as unknown as { chrome: unknown }).chrome = {
    storage: { local: storage },
  };
}

function makeEvent(overrides: Partial<QueuedEvent> = {}): QueuedEvent {
  return {
    eventId: "evt_1", clientSequence: 1, handoffSessionId: "hs_1",
    eventType: "field_detected", eventPayload: {}, observedAt: "2026-08-24T00:00:00Z",
    ...overrides,
  };
}

// Manually resolvable/controllable barrier, so a test can pause a fake
// storage operation mid-flight and release it at a chosen moment.
function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => { resolve = r; });
  return { promise, resolve };
}

describe("ChromeEventStore concurrency", () => {
  let storage: BarrieredFakeStorage;
  let store: ChromeEventStore;

  beforeEach(() => {
    storage = new BarrieredFakeStorage();
    installFakeChromeStorage(storage);
    store = new ChromeEventStore();
  });

  it("concurrent adds preserve every distinct event (fails against a naive getAll-then-set add())", async () => {
    // Fire many adds without awaiting any of them individually first --
    // this is exactly the shape runContentScript produces: several
    // sendMessage() calls back-to-back with no await between them,
    // each eventually triggering its own store.add().
    const events = Array.from({ length: 20 }, (_, i) =>
      makeEvent({ eventId: `evt_${i}`, clientSequence: i }),
    );
    await Promise.all(events.map((event) => store.add(event)));

    const all = await store.getAll();
    expect(all).toHaveLength(20);
    expect(new Set(all.map((e) => e.eventId))).toEqual(
      new Set(events.map((e) => e.eventId)),
    );
  });

  it("add racing with remove preserves the newly added unrelated event", async () => {
    await store.add(makeEvent({ eventId: "evt_existing" }));

    // Start a remove() of the existing event and an add() of a brand new,
    // unrelated event at the same time -- neither should clobber the
    // other's effect regardless of which one's underlying read happened
    // to run first.
    await Promise.all([
      store.remove("evt_existing"),
      store.add(makeEvent({ eventId: "evt_new" })),
    ]);

    const all = await store.getAll();
    const ids = all.map((e) => e.eventId);
    expect(ids).not.toContain("evt_existing");
    expect(ids).toContain("evt_new");
    expect(all).toHaveLength(1);
  });

  it("removing one event never removes another event added concurrently, forcing genuine overlap with a barrier", async () => {
    await store.add(makeEvent({ eventId: "evt_a" }));

    // Gate the underlying storage so add("evt_b")'s read (of the current
    // array containing only evt_a) is captured BEFORE remove("evt_a")'s
    // write can land -- reproducing the exact interleaving that a naive
    // (unserialized) implementation loses evt_a's removal or evt_b's
    // addition under, without relying on incidental scheduling luck.
    const gate = deferred();
    storage.gate = gate.promise;

    const removePromise = store.remove("evt_a");
    const addPromise = store.add(makeEvent({ eventId: "evt_b", clientSequence: 2 }));

    // Let both operations' gated storage calls proceed now that both are
    // in flight.
    gate.resolve();
    storage.gate = null;
    await Promise.all([removePromise, addPromise]);

    const all = await store.getAll();
    const ids = all.map((e) => e.eventId).sort();
    expect(ids).toEqual(["evt_b"]);
  });
});
