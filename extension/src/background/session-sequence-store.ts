// Production per-session client-sequence state, keyed by
// handoffSessionId — replaces the Sub-project-1-era manual-test single
// key (handoff_manual_test_client_sequence), which could only ever
// track one session at a time and was never meant to survive into
// production use. Storing sequence state per session id (rather than
// a single "current" slot) means an MV3 service-worker restart can
// always resume the correct session's sequence even if a different
// session was active more recently, and a stale entry for an old,
// already-terminal session is simply never read again (cleanup on
// terminal transition is a later task's concern, not this one's).
const SEQUENCE_KEY_PREFIX = "handoff_session_sequence:";

export class SessionSequenceStore {
  private key(handoffSessionId: string): string {
    return `${SEQUENCE_KEY_PREFIX}${handoffSessionId}`;
  }

  async get(handoffSessionId: string): Promise<number> {
    const key = this.key(handoffSessionId);
    const result = await chrome.storage.local.get(key);
    return (result[key] as number | undefined) ?? 0;
  }

  async set(handoffSessionId: string, sequence: number): Promise<void> {
    await chrome.storage.local.set({ [this.key(handoffSessionId)]: sequence });
  }
}
