import { MessageRouter } from "./message-router";
import type { DurableEventQueue } from "./event-queue";
import type { BoundSession } from "./session-orchestration";

// Per-tab and per-session runtime state, replacing the single
// module-level `boundSession`/`router` slots from Task 9 — those would
// silently corrupt one tab's in-flight session state the moment a
// second tab associated a different handoff session (design spec /
// Task 12: "two simultaneous employer tabs using two different handoff
// sessions cannot share/corrupt sequence state").
//
// Three independent maps, keyed differently on purpose:
//   - sessionByTab:  tabId -> the session that tab currently holds
//                    (a tab always has exactly one CURRENT session).
//   - routerBySessionId: handoffSessionId -> its MessageRouter/sequence
//                    state, looked up by session id alone so a queued
//                    event can be routed/flushed correctly even if its
//                    originating tab has since navigated away or
//                    re-associated a different session.
//   - tokenBySessionId: handoffSessionId -> its live session token, kept
//                    independent of sessionByTab so a stale/rebound tab
//                    entry can never cause an in-flight event for an
//                    older (but still valid) session to lose its token.
export class SessionRegistry {
  private readonly sessionByTab = new Map<number, BoundSession>();
  private readonly routerBySessionId = new Map<string, MessageRouter>();
  private readonly tokenBySessionId = new Map<string, string>();

  constructor(
    private readonly eventQueue: DurableEventQueue,
    private readonly getPersistedSequence: (handoffSessionId: string) => Promise<number>,
  ) {}

  bindTab(tabId: number, session: BoundSession): void {
    this.sessionByTab.set(tabId, session);
    this.tokenBySessionId.set(session.sessionId, session.sessionToken);
  }

  sessionForTab(tabId: number): BoundSession | undefined {
    return this.sessionByTab.get(tabId);
  }

  // Looks up (or lazily creates) the router for a specific handoffSessionId
  // — never the "current" tab's session, so a call resolved via
  // sender.tab.id always affects only that tab's own session's router,
  // regardless of what any other tab is concurrently doing.
  async ensureRouterForSession(handoffSessionId: string): Promise<MessageRouter> {
    const existing = this.routerBySessionId.get(handoffSessionId);
    if (existing) return existing;

    const startingClientSequence = await this.getPersistedSequence(handoffSessionId);
    const router = new MessageRouter(this.eventQueue, handoffSessionId, startingClientSequence);
    this.routerBySessionId.set(handoffSessionId, router);
    return router;
  }

  // Token lookup for QueuedEvent delivery: resolves strictly by the
  // event's OWN handoffSessionId, never by "whichever tab is active" or
  // any other implicit current-session notion — this is what makes a
  // flush during two concurrent sessions route each event with the
  // correct session's own token instead of whatever session happened to
  // be bound most recently.
  tokenForSession(handoffSessionId: string): string | undefined {
    return this.tokenBySessionId.get(handoffSessionId);
  }

  // Terminal-session cleanup (e.g. after confirm-submission) removes
  // only that one session's router and token — every other session's
  // router/sequence/token state is untouched.
  releaseSession(handoffSessionId: string): void {
    this.routerBySessionId.delete(handoffSessionId);
    this.tokenBySessionId.delete(handoffSessionId);
  }

  releaseTab(tabId: number): void {
    this.sessionByTab.delete(tabId);
  }
}
