import { describe, expect, it, vi } from "vitest";
import { SessionRegistry } from "../src/background/session-registry";
import { DurableEventQueue } from "../src/background/event-queue";
import type { BoundSession } from "../src/background/session-orchestration";

function fakeEventQueue() {
  return new DurableEventQueue(
    { getAll: vi.fn().mockResolvedValue([]), add: vi.fn(), remove: vi.fn() },
    vi.fn().mockResolvedValue(true),
  );
}

function session(overrides: Partial<BoundSession> = {}): BoundSession {
  return {
    sessionId: "hs_1", sessionToken: "tok_1",
    workspaceId: "ws_1", packArtifactId: "art_1",
    ...overrides,
  };
}

describe("SessionRegistry", () => {
  it("creates independent routers for two different session ids", async () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    const routerA = await registry.ensureRouterForSession("session_a");
    const routerB = await registry.ensureRouterForSession("session_b");
    expect(routerA).not.toBe(routerB);
    expect(routerA.handoffSessionId).toBe("session_a");
    expect(routerB.handoffSessionId).toBe("session_b");
  });

  it("returns the same router for a repeated call with the same session id", async () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    const first = await registry.ensureRouterForSession("session_a");
    const second = await registry.ensureRouterForSession("session_a");
    expect(first).toBe(second);
  });

  it("resumes clientSequence numbering for a session from persisted storage", async () => {
    const getPersistedSequence = vi.fn().mockImplementation(async (id: string) =>
      id === "session_a" ? 5 : 0,
    );
    const registry = new SessionRegistry(fakeEventQueue(), getPersistedSequence);
    const router = await registry.ensureRouterForSession("session_a");
    expect(router.clientSequence).toBe(5);
  });

  it("simulated service-worker restart: a fresh registry re-reads persisted sequence independently per session", async () => {
    const getPersistedSequence = vi.fn().mockImplementation(async (id: string) => {
      if (id === "session_a") return 5;
      if (id === "session_b") return 12;
      return 0;
    });
    // A restart discards all in-memory registry state — simulated here
    // by constructing a brand new SessionRegistry rather than reusing one.
    const registry = new SessionRegistry(fakeEventQueue(), getPersistedSequence);
    const routerA = await registry.ensureRouterForSession("session_a");
    const routerB = await registry.ensureRouterForSession("session_b");
    expect(routerA.clientSequence).toBe(5);
    expect(routerB.clientSequence).toBe(12);
  });

  it("two tabs bound to two different sessions have independent tokens", () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    registry.bindTab(1, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    registry.bindTab(2, session({ sessionId: "hs_b", sessionToken: "tok_b" }));

    expect(registry.tokenForSession("hs_a")).toBe("tok_a");
    expect(registry.tokenForSession("hs_b")).toBe("tok_b");
    expect(registry.sessionForTab(1)?.sessionId).toBe("hs_a");
    expect(registry.sessionForTab(2)?.sessionId).toBe("hs_b");
  });

  it("advancing one session's router does not affect another session's router or token", async () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    registry.bindTab(1, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    registry.bindTab(2, session({ sessionId: "hs_b", sessionToken: "tok_b" }));

    const routerA = await registry.ensureRouterForSession("hs_a");
    await routerA.route({ type: "field_detected", pageFieldKey: "k1", observedAt: "t" } as never);
    await routerA.route({ type: "field_detected", pageFieldKey: "k2", observedAt: "t" } as never);

    const routerB = await registry.ensureRouterForSession("hs_b");

    expect(routerA.clientSequence).toBe(2);
    expect(routerB.clientSequence).toBe(0);
    expect(registry.tokenForSession("hs_a")).toBe("tok_a");
    expect(registry.tokenForSession("hs_b")).toBe("tok_b");
  });

  it("releasing one session's state does not affect another active session's router or token", async () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    registry.bindTab(1, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    registry.bindTab(2, session({ sessionId: "hs_b", sessionToken: "tok_b" }));
    await registry.ensureRouterForSession("hs_a");
    const routerB = await registry.ensureRouterForSession("hs_b");

    registry.releaseSession("hs_a");

    expect(registry.tokenForSession("hs_a")).toBeUndefined();
    expect(registry.tokenForSession("hs_b")).toBe("tok_b");
    // hs_b's router identity is unaffected by releasing hs_a.
    expect(await registry.ensureRouterForSession("hs_b")).toBe(routerB);
  });

  it("rebinding a tab to a new session releases the prior session's router/token when no other tab holds it", async () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    registry.bindTab(1, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    await registry.ensureRouterForSession("hs_a");
    // Same tab later re-associates a different session (e.g. user
    // clicked "Apply with extension" again for a different workspace).
    registry.bindTab(1, session({ sessionId: "hs_b", sessionToken: "tok_b" }));

    expect(registry.sessionForTab(1)?.sessionId).toBe("hs_b");
    // hs_a is no longer referenced by any tab, so its router/token are
    // cleaned up as part of the rebind — no stale entry is left behind.
    expect(registry.tokenForSession("hs_a")).toBeUndefined();
    expect(registry.tokenForSession("hs_b")).toBe("tok_b");
  });

  it("rebinding a tab does not release a prior session still held by another tab", async () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    registry.bindTab(1, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    registry.bindTab(2, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    await registry.ensureRouterForSession("hs_a");

    // Tab 1 moves to a different session; tab 2 still legitimately holds hs_a.
    registry.bindTab(1, session({ sessionId: "hs_b", sessionToken: "tok_b" }));

    expect(registry.tokenForSession("hs_a")).toBe("tok_a");
    expect(registry.sessionForTab(2)?.sessionId).toBe("hs_a");
  });

  it("closing tab A releases A's session without affecting tab B's independent session", async () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    registry.bindTab(1, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    registry.bindTab(2, session({ sessionId: "hs_b", sessionToken: "tok_b" }));
    const routerB = await registry.ensureRouterForSession("hs_b");
    await registry.ensureRouterForSession("hs_a");

    registry.releaseTab(1);

    expect(registry.sessionForTab(1)).toBeUndefined();
    expect(registry.tokenForSession("hs_a")).toBeUndefined();
    // Tab B / session hs_b is completely untouched.
    expect(registry.sessionForTab(2)?.sessionId).toBe("hs_b");
    expect(registry.tokenForSession("hs_b")).toBe("tok_b");
    expect(await registry.ensureRouterForSession("hs_b")).toBe(routerB);
  });

  it("closing a tab does not release its session while another tab still holds the same session", async () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    registry.bindTab(1, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    registry.bindTab(2, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    const router = await registry.ensureRouterForSession("hs_a");

    registry.releaseTab(1);

    expect(registry.sessionForTab(1)).toBeUndefined();
    expect(registry.sessionForTab(2)?.sessionId).toBe("hs_a");
    // hs_a is still referenced by tab 2, so its router/token survive.
    expect(registry.tokenForSession("hs_a")).toBe("tok_a");
    expect(await registry.ensureRouterForSession("hs_a")).toBe(router);
  });

  it("closing a tab that never bound a session is a no-op", () => {
    const registry = new SessionRegistry(fakeEventQueue(), async () => 0);
    registry.bindTab(2, session({ sessionId: "hs_b", sessionToken: "tok_b" }));

    expect(() => registry.releaseTab(999)).not.toThrow();
    expect(registry.sessionForTab(2)?.sessionId).toBe("hs_b");
    expect(registry.tokenForSession("hs_b")).toBe("tok_b");
  });

  it("cleanup triggered by closing one tab cannot clear another active session's sequence state", async () => {
    const getPersistedSequence = vi.fn().mockImplementation(async (id: string) =>
      id === "hs_b" ? 7 : 0,
    );
    const registry = new SessionRegistry(fakeEventQueue(), getPersistedSequence);
    registry.bindTab(1, session({ sessionId: "hs_a", sessionToken: "tok_a" }));
    registry.bindTab(2, session({ sessionId: "hs_b", sessionToken: "tok_b" }));
    await registry.ensureRouterForSession("hs_a");

    registry.releaseTab(1);

    // hs_b's router is created fresh here (first ensure call for it), and
    // must still resume from its own persisted sequence untouched by
    // hs_a's tab having closed moments earlier.
    const routerB = await registry.ensureRouterForSession("hs_b");
    expect(routerB.clientSequence).toBe(7);
  });
});
