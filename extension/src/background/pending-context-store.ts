// The ONLY module that reads or writes the pending handoff-launch
// context. Uses chrome.storage.session (not .local) deliberately: this
// data is only ever meaningful for a few minutes between "Apply with
// extension" and the user clicking the toolbar icon on the resulting
// tab — a full browser restart clearing it is intended, not a bug.
export interface PendingHandoffContext {
  workspaceId: string;
  packArtifactId: string;
  targetUrl: string;
  requestedAt: number;
}

const PENDING_CONTEXT_KEY = "handoff_pending_context";
const PENDING_CONTEXT_TTL_MS = 5 * 60 * 1000;

export class PendingContextStore {
  async set(context: PendingHandoffContext): Promise<void> {
    // A new call always replaces whatever was there — deterministic,
    // no merge, no append. Exactly one pending context can exist at a
    // time.
    await chrome.storage.session.set({ [PENDING_CONTEXT_KEY]: context });
  }

  // Deliberately named peek, not get/consume: does NOT clear a valid
  // context on read. Consume-on-success is Task 9's responsibility
  // (clearing only after a handoff session is actually associated),
  // not this store's.
  async peek(now: () => number = Date.now): Promise<PendingHandoffContext | null> {
    const result = await chrome.storage.session.get(PENDING_CONTEXT_KEY);
    const context = result[PENDING_CONTEXT_KEY] as PendingHandoffContext | undefined;
    if (!context) return null;
    if (now() - context.requestedAt > PENDING_CONTEXT_TTL_MS) {
      await this.clear();
      return null;
    }
    return context;
  }

  async clear(): Promise<void> {
    await chrome.storage.session.remove(PENDING_CONTEXT_KEY);
  }
}
