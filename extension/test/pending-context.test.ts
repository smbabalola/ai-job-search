import { describe, expect, it, vi } from "vitest";
import { PendingContextStore } from "../src/background/pending-context-store";

describe("PendingContextStore", () => {
  it("stores and returns a context via peek", async () => {
    const store = new PendingContextStore();
    const setMock = vi.fn().mockResolvedValue(undefined);
    const getMock = vi.fn();
    vi.stubGlobal("chrome", { storage: { session: { set: setMock, get: getMock, remove: vi.fn() } } });
    const context = {
      workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x.test/apply",
      requestedAt: Date.now(),
    };
    getMock.mockResolvedValue({ handoff_pending_context: context });

    await store.set(context);
    expect(setMock).toHaveBeenCalledWith({ handoff_pending_context: context });
    const peeked = await store.peek();
    expect(peeked).toEqual(context);
    vi.unstubAllGlobals();
  });

  it("replacing a prior pending context is deterministic (last set wins, no merge)", async () => {
    const store = new PendingContextStore();
    const setMock = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("chrome", { storage: { session: { set: setMock, get: vi.fn(), remove: vi.fn() } } });

    const first = { workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://a.test", requestedAt: 1 };
    const second = { workspaceId: "ws_2", packArtifactId: "art_2", targetUrl: "https://b.test", requestedAt: 2 };
    await store.set(first);
    await store.set(second);

    expect(setMock).toHaveBeenNthCalledWith(1, { handoff_pending_context: first });
    expect(setMock).toHaveBeenNthCalledWith(2, { handoff_pending_context: second });
    vi.unstubAllGlobals();
  });

  it("survives a simulated service-worker recreation (a fresh PendingContextStore instance still reads the same persisted value)", async () => {
    const context = {
      workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x.test/apply",
      requestedAt: Date.now(),
    };
    const getMock = vi.fn().mockResolvedValue({ handoff_pending_context: context });
    vi.stubGlobal("chrome", { storage: { session: { set: vi.fn(), get: getMock, remove: vi.fn() } } });

    // Simulates an MV3 service-worker restart: a brand-new store
    // instance (no in-memory state carried over) still sees the
    // context, because it was never held anywhere but
    // chrome.storage.session.
    const recreatedStore = new PendingContextStore();
    const peeked = await recreatedStore.peek();

    expect(peeked).toEqual(context);
    vi.unstubAllGlobals();
  });

  it("treats an expired context as absent and clears it", async () => {
    const removeMock = vi.fn().mockResolvedValue(undefined);
    const old = {
      workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x.test/apply",
      requestedAt: Date.now() - 6 * 60 * 1000,
    };
    vi.stubGlobal("chrome", { storage: { session: {
      get: vi.fn().mockResolvedValue({ handoff_pending_context: old }), remove: removeMock, set: vi.fn(),
    } } });

    const store = new PendingContextStore();
    const peeked = await store.peek();

    expect(peeked).toBeNull();
    expect(removeMock).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("does not clear on peek when the context is still valid", async () => {
    const removeMock = vi.fn().mockResolvedValue(undefined);
    const fresh = {
      workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x.test/apply",
      requestedAt: Date.now(),
    };
    vi.stubGlobal("chrome", { storage: { session: {
      get: vi.fn().mockResolvedValue({ handoff_pending_context: fresh }), remove: removeMock, set: vi.fn(),
    } } });

    const store = new PendingContextStore();
    await store.peek();
    await store.peek(); // a second read must still not clear it

    expect(removeMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("clear() removes the stored context", async () => {
    const removeMock = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("chrome", { storage: { session: { get: vi.fn(), remove: removeMock, set: vi.fn() } } });

    const store = new PendingContextStore();
    await store.clear();

    expect(removeMock).toHaveBeenCalledWith("handoff_pending_context");
    vi.unstubAllGlobals();
  });
});
