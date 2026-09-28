import type { QueuedEvent } from "./event-queue";

const BASE_URL = "http://127.0.0.1:8420";

export interface DiscoveredSession {
  id: string;
  workspace_id: string;
  pack_artifact_id: string;
  target_domain: string;
  status: string;
  started_at: string;
  last_activity_at: string;
  [key: string]: unknown;
}

// The ONLY module that makes HTTP calls to the JobSearch server (design
// spec Section 4 / Section 17 — content scripts never call this
// directly, only through messages relayed by the background worker).
export class ServerClient {
  constructor(private readonly getCredential: () => Promise<string | null>) {}

  // Durable-credential authorization — used only for pairing exchange
  // (implicitly, via no header at all) and the three extension-scoped
  // routes that are the sole entry points into session identity:
  // start, discover, resume (design spec Section 3.2 / Section 5.2).
  // Every other session-scoped call uses sessionHeaders() below instead.
  private async headers(): Promise<Record<string, string>> {
    const credential = await this.getCredential();
    if (!credential) throw new Error("extension is not paired");
    return { "X-Handoff-Credential": credential, "Content-Type": "application/json" };
  }

  // Session-token authorization — used for every call scoped to a
  // specific, already-identified handoff session. Never falls back to
  // the durable credential: a stale/rotated session token must fail
  // outright rather than silently re-authorizing via a different
  // credential.
  private sessionHeaders(sessionToken: string): Record<string, string> {
    return { "X-Handoff-Session-Token": sessionToken, "Content-Type": "application/json" };
  }

  async exchangePairing(
    oneTimeSecret: string,
  ): Promise<{ credentialId: string; durableSecret: string }> {
    const response = await fetch(`${BASE_URL}/api/handoff/pairing/exchange`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ one_time_secret: oneTimeSecret }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail ?? `pairing failed: ${response.status}`);
    }
    const result = await response.json();
    return { credentialId: result.credential_id, durableSecret: result.durable_secret };
  }

  async startSession(body: {
    workspaceId: string; packArtifactId: string; targetUrl: string;
    targetDomain: string; atsAdapterId: string; atsAdapterVersion: string;
  }): Promise<{ id: string; sessionToken: string }> {
    const response = await fetch(`${BASE_URL}/api/handoff/sessions`, {
      method: "POST", headers: await this.headers(),
      body: JSON.stringify({
        workspace_id: body.workspaceId, pack_artifact_id: body.packArtifactId,
        target_url: body.targetUrl, target_domain: body.targetDomain,
        ats_adapter_id: body.atsAdapterId, ats_adapter_version: body.atsAdapterVersion,
      }),
    });
    if (!response.ok) throw new Error(`failed to start handoff session: ${response.status}`);
    const result = await response.json();
    return { id: result.id, sessionToken: result.session_token };
  }

  // Metadata-only listing (design spec Section 5.2) — never returns a
  // session token for any row. The caller is responsible for deciding
  // which (if any) resumable candidate to actually resume; this method
  // does no filtering or selection of its own.
  async discoverSessions(workspaceId: string, targetDomain: string): Promise<DiscoveredSession[]> {
    const params = new URLSearchParams({ workspace_id: workspaceId, target_domain: targetDomain });
    const response = await fetch(`${BASE_URL}/api/handoff/sessions/discover?${params}`, {
      method: "GET", headers: await this.headers(),
    });
    if (!response.ok) throw new Error(`failed to discover handoff sessions: ${response.status}`);
    const result = await response.json();
    return result.sessions;
  }

  // The only rotation path for an existing session's token (design spec
  // Section 3.2) — authorized with the durable credential, exactly like
  // startSession, never with a prior session token.
  async resumeSession(sessionId: string): Promise<{ sessionToken: string }> {
    const response = await fetch(`${BASE_URL}/api/handoff/sessions/${sessionId}/resume`, {
      method: "POST", headers: await this.headers(),
    });
    if (!response.ok) throw new Error(`failed to resume handoff session: ${response.status}`);
    const result = await response.json();
    return { sessionToken: result.session_token };
  }

  async sendEvent(event: QueuedEvent, sessionToken: string): Promise<boolean> {
    const response = await fetch(
      `${BASE_URL}/api/handoff/sessions/${event.handoffSessionId}/events`,
      {
        method: "POST", headers: this.sessionHeaders(sessionToken),
        body: JSON.stringify({
          event_id: event.eventId, event_type: event.eventType,
          event_payload: event.eventPayload,
          normalized_field_type: event.normalizedFieldType ?? null,
          page_field_key: event.pageFieldKey ?? null,
          observed_at: event.observedAt,
        }),
      },
    );
    return response.ok;
  }

  async confirmSubmission(
    handoffSessionId: string, markWorkflowApplied: boolean, sessionToken: string,
  ): Promise<unknown> {
    const response = await fetch(
      `${BASE_URL}/api/handoff/sessions/${handoffSessionId}/confirm-submission`,
      {
        method: "POST", headers: this.sessionHeaders(sessionToken),
        body: JSON.stringify({ mark_workflow_applied: markWorkflowApplied }),
      },
    );
    if (!response.ok) throw new Error(`failed to confirm submission: ${response.status}`);
    return response.json();
  }
}
