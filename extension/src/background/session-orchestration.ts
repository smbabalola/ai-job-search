// Pure decision logic for the discover -> resume-or-start orchestration
// (no chrome.* calls here, so this module is safely importable in the
// test environment — matching the pending-context-validation.ts
// precedent). The wiring that calls chrome.tabs/chrome.storage lives in
// background/index.ts; this module only decides what to do given
// already-fetched inputs.
import type { PendingHandoffContext } from "./pending-context-store";
import type { DiscoveredSession, ServerClient } from "./server-client";

export const HANDOFF_SESSION_INACTIVITY_TIMEOUT_MS = 2 * 60 * 60 * 1000;

export interface BoundSession {
  sessionId: string;
  sessionToken: string;
  workspaceId: string;
  packArtifactId: string;
}

export class WrongActiveTabError extends Error {
  constructor() {
    super("the active tab's domain does not match the pending handoff context's target URL");
    this.name = "WrongActiveTabError";
  }
}

export class NoPendingContextError extends Error {
  constructor() {
    super("no valid pending handoff context (click Apply with extension first)");
    this.name = "NoPendingContextError";
  }
}

function extractDomain(url: string): string | null {
  try {
    return new URL(url).hostname;
  } catch {
    return null;
  }
}

// Validates the active employer tab's URL against the pending context's
// targetUrl before any server call is made — a stale or hijacked
// pending context must never be used against the wrong page (contract
// requirement: "validate current employer tab against targetUrl").
export function isActiveTabOnPendingTarget(
  activeTabUrl: string | undefined, pendingContext: PendingHandoffContext,
): boolean {
  if (!activeTabUrl) return false;
  const activeDomain = extractDomain(activeTabUrl);
  const targetDomain = extractDomain(pendingContext.targetUrl);
  return activeDomain !== null && activeDomain === targetDomain;
}

// Deterministic exact-match filter: a session is resumable ONLY if it
// matches the pending workspaceId AND the exact packArtifactId AND the
// target domain, AND it has not exceeded the 2-hour inactivity window.
// A session for the same workspace/domain but a different pack is never
// a candidate — this is the single most important invariant of this
// module (contract: "must never be resumed").
export function filterExactMatchingSessions(
  candidates: DiscoveredSession[],
  pendingContext: PendingHandoffContext,
  targetDomain: string,
  now: () => number = Date.now,
): DiscoveredSession[] {
  return candidates.filter((session) => {
    if (session.status !== "in_progress") return false;
    if (session.workspace_id !== pendingContext.workspaceId) return false;
    if (session.pack_artifact_id !== pendingContext.packArtifactId) return false;
    if (session.target_domain !== targetDomain) return false;
    const lastActivity = Date.parse(session.last_activity_at);
    if (Number.isNaN(lastActivity)) return false;
    if (now() - lastActivity > HANDOFF_SESSION_INACTIVITY_TIMEOUT_MS) return false;
    return true;
  });
}

// Deterministic policy for multiple exact matches: the most recently
// active session wins (ties broken by started_at, then id, so the
// choice is fully deterministic and never depends on arbitrary API
// ordering). This is a real policy decision, not an accident of
// discover's own ORDER BY — the server's ordering is not a contract
// this module relies on.
export function chooseSessionToResume(matches: DiscoveredSession[]): DiscoveredSession | null {
  if (matches.length === 0) return null;
  const sorted = [...matches].sort((a, b) => {
    const activityDiff = Date.parse(b.last_activity_at) - Date.parse(a.last_activity_at);
    if (activityDiff !== 0) return activityDiff;
    const startedDiff = Date.parse(b.started_at) - Date.parse(a.started_at);
    if (startedDiff !== 0) return startedDiff;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
  return sorted[0];
}

export interface AdapterIdentity {
  atsAdapterId: string;
  atsAdapterVersion: string;
}

// Full orchestration: resume only an unexpired session whose
// workspace_id AND pack_artifact_id AND target domain all match the
// pending context exactly; otherwise start a new session pinned to the
// pending context's exact packArtifactId. Never fetches CandidateSnapshot
// and never clears the pending context itself — the caller clears it
// only after this resolves successfully (contract requirement).
export async function associateHandoffSession(
  pendingContext: PendingHandoffContext,
  targetDomain: string,
  adapter: AdapterIdentity,
  serverClient: ServerClient,
  now: () => number = Date.now,
): Promise<BoundSession> {
  const discovered = await serverClient.discoverSessions(pendingContext.workspaceId, targetDomain);
  const exactMatches = filterExactMatchingSessions(discovered, pendingContext, targetDomain, now);
  const chosen = chooseSessionToResume(exactMatches);

  if (chosen) {
    const { sessionToken } = await serverClient.resumeSession(chosen.id);
    return {
      sessionId: chosen.id, sessionToken,
      workspaceId: pendingContext.workspaceId, packArtifactId: pendingContext.packArtifactId,
    };
  }

  const { id, sessionToken } = await serverClient.startSession({
    workspaceId: pendingContext.workspaceId, packArtifactId: pendingContext.packArtifactId,
    targetUrl: pendingContext.targetUrl, targetDomain,
    atsAdapterId: adapter.atsAdapterId, atsAdapterVersion: adapter.atsAdapterVersion,
  });
  return {
    sessionId: id, sessionToken,
    workspaceId: pendingContext.workspaceId, packArtifactId: pendingContext.packArtifactId,
  };
}
