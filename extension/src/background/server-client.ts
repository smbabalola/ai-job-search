import type { QueuedEvent } from "./event-queue";
import { BACKEND_ORIGIN } from "../shared/backend";

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

// The device credential (Bundle 7 spec X2): bearer headers for every call, and
// the reaction to a 401 (drop an expired access token / wipe a revoked device).
export interface DeviceAuth {
  authHeaders(): Promise<Record<string, string>>;
  handleUnauthorized(errorCode: string | undefined): Promise<void>;
}

// The ONLY module that makes handoff HTTP calls to the JobSearch server (design
// spec Section 4 / Section 17 — content scripts never call this directly, only
// through messages relayed by the background worker). Pairing and token refresh
// live in TokenClient.
export class ServerClient {
  constructor(private readonly auth: DeviceAuth, private readonly baseUrl: string = BACKEND_ORIGIN) {}

  // Device-only authorization: the entry points into session identity
  // (start, discover, resume).
  private async headers(): Promise<Record<string, string>> {
    return { ...(await this.auth.authHeaders()), "Content-Type": "application/json" };
  }

  // Session-scoped calls carry the session token AND the device's bearer: the
  // server honours a session token only for the device's own account. Never
  // falls back to device-only authorization.
  private async sessionHeaders(sessionToken: string): Promise<Record<string, string>> {
    return { ...(await this.auth.authHeaders()), "X-Handoff-Session-Token": sessionToken,
             "Content-Type": "application/json" };
  }

  private async fetchChecked(url: string, init: RequestInit): Promise<Response> {
    const response = await fetch(url, init);
    if (response.status === 401) {
      const body = await response.clone().json().catch(() => ({}));
      await this.auth.handleUnauthorized(body.error ?? body.detail?.error);
    }
    return response;
  }

  async startSession(body: {
    handoffTicket: string; workspaceId: string; packArtifactId: string; targetUrl: string;
    targetDomain: string; atsAdapterId: string; atsAdapterVersion: string;
  }): Promise<{ id: string; sessionToken: string }> {
    const response = await this.fetchChecked(`${this.baseUrl}/api/handoff/sessions`, {
      method: "POST", headers: await this.headers(),
      body: JSON.stringify({
        handoff_ticket: body.handoffTicket,
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
    const response = await this.fetchChecked(`${this.baseUrl}/api/handoff/sessions/discover?${params}`, {
      method: "GET", headers: await this.headers(),
    });
    if (!response.ok) throw new Error(`failed to discover handoff sessions: ${response.status}`);
    const result = await response.json();
    return result.sessions;
  }

  // The only rotation path for an existing session's token (design spec
  // Section 3.2) — authorized by the device, never by a prior session token.
  async resumeSession(sessionId: string): Promise<{ sessionToken: string }> {
    const response = await this.fetchChecked(`${this.baseUrl}/api/handoff/sessions/${sessionId}/resume`, {
      method: "POST", headers: await this.headers(),
    });
    if (!response.ok) throw new Error(`failed to resume handoff session: ${response.status}`);
    const result = await response.json();
    return { sessionToken: result.session_token };
  }

  async sendEvent(event: QueuedEvent, sessionToken: string): Promise<boolean> {
    const response = await this.fetchChecked(
      `${this.baseUrl}/api/handoff/sessions/${event.handoffSessionId}/events`,
      {
        method: "POST", headers: await this.sessionHeaders(sessionToken),
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
    const response = await this.fetchChecked(
      `${this.baseUrl}/api/handoff/sessions/${handoffSessionId}/confirm-submission`,
      {
        method: "POST", headers: await this.sessionHeaders(sessionToken),
        body: JSON.stringify({ mark_workflow_applied: markWorkflowApplied }),
      },
    );
    if (!response.ok) throw new Error(`failed to confirm submission: ${response.status}`);
    return response.json();
  }
}
