// Bundle 6D-B typed client for the extension fill routes (spec §20), under
// /api/handoff/sessions/{sid}/fill with the handoff session token. Only the
// intent response ever carries a cleartext value, and it is used for one
// action and never stored.

import type { ObservationV1 } from "./observation-types";
import type { ActionKind, Envelope, Outcome, PlanDocument } from "./executor";

export const FILL_SERVER_BASE_URL = "http://127.0.0.1:8420";

export class FillServerError extends Error {
  constructor(readonly status: number, readonly detail: string) {
    super(`fill server ${status}: ${detail}`);
    this.name = "FillServerError";
  }
}

export interface StepResult { state: string; reason?: string | null; [key: string]: unknown }

export interface PlanStatusAction {
  page_field_key: string;
  field_fingerprint: string;
  action_kind: ActionKind;
  rendered_value_hash: string | null;
  document: PlanDocument | null;
  document_kind: string | null;
}

export interface PlanStatus {
  run_id: string;
  state: string;
  reason: string | null;
  grant_id: string | null;
  ruleset_hash: string | null;
  plan_hash: string | null;
  structure_fingerprint: string | null;
  actions: PlanStatusAction[];
}

export interface Precheck {
  ruleset_hash: string;
  structure_fingerprint: string;
  field_fingerprint: string;
  siblings_contained: boolean;
}

export interface IntentResponse { envelope: Envelope | null; stop_reason: string | null; detail: Record<string, unknown> }

export interface LocalDocument { bytes: Uint8Array<ArrayBuffer>; filename: string; mediaType: string; sha256: string }

export interface FillServer {
  startRun(identity: { executor_instance_id: string; browser_session_id: string; execution_tab_id: number })
    : Promise<{ id: string }>;
  observation(runId: string, phase: string, observation: ObservationV1, actionIndex?: number | null): Promise<StepResult>;
  planStatus(runId: string): Promise<PlanStatus>;
  quarantine(runId: string, phase: string, rulesetHash: string | null): Promise<StepResult>;
  grant(runId: string): Promise<{ granted: boolean; grant_id: string | null; stop_reason: string | null }>;
  intent(runId: string, index: number, precheck: Precheck): Promise<IntentResponse>;
  outcome(runId: string, index: number, body: { envelope_id: string | null; outcome: Outcome;
    readback_hash: string | null; post_observation: ObservationV1 }): Promise<StepResult>;
  unknown(runId: string, index: number): Promise<StepResult>;
  heartbeat(runId: string): Promise<{ state: string; lease_expired: boolean }>;
  detection(runId: string, kind: string, detail: Record<string, unknown>): Promise<StepResult>;
  final(runId: string, observation: ObservationV1): Promise<StepResult>;
  stop(runId: string, reason: string, detail: Record<string, unknown>): Promise<StepResult>;
  document(kind: string): Promise<LocalDocument>;
}

type Fetch = typeof fetch;

export class HttpFillServer implements FillServer {
  constructor(private readonly sessionId: string, private readonly sessionToken: string,
              private readonly fetchImpl: Fetch = (...args) => fetch(...args),
              private readonly baseUrl: string = FILL_SERVER_BASE_URL) {}

  private url(path: string): string {
    return `${this.baseUrl}/api/handoff/sessions/${this.sessionId}/fill${path}`;
  }

  private async call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    const response = await this.fetchImpl(this.url(path), {
      method, headers: { "X-Handoff-Session-Token": this.sessionToken, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail ?? null);
      throw new FillServerError(response.status, detail);
    }
    return payload as T;
  }

  startRun(identity: { executor_instance_id: string; browser_session_id: string; execution_tab_id: number }) {
    return this.call<{ id: string }>("POST", "/runs", identity);
  }

  observation(runId: string, phase: string, observation: ObservationV1, actionIndex: number | null = null) {
    return this.call<StepResult>("POST", `/runs/${runId}/observations`, { phase, action_index: actionIndex,
      observation });
  }

  planStatus(runId: string) {
    return this.call<PlanStatus>("GET", `/runs/${runId}/plan-status`);
  }

  quarantine(runId: string, phase: string, rulesetHash: string | null) {
    return this.call<StepResult>("POST", `/runs/${runId}/quarantine`, { phase, ruleset_hash: rulesetHash });
  }

  grant(runId: string) {
    return this.call<{ granted: boolean; grant_id: string | null; stop_reason: string | null }>(
      "POST", `/runs/${runId}/grant`);
  }

  intent(runId: string, index: number, precheck: Precheck) {
    return this.call<IntentResponse>("POST", `/runs/${runId}/actions/${index}/intent`, { precheck });
  }

  outcome(runId: string, index: number, body: { envelope_id: string | null; outcome: Outcome;
    readback_hash: string | null; post_observation: ObservationV1 }) {
    return this.call<StepResult>("POST", `/runs/${runId}/actions/${index}/outcome`, body);
  }

  unknown(runId: string, index: number) {
    return this.call<StepResult>("POST", `/runs/${runId}/actions/${index}/unknown`);
  }

  heartbeat(runId: string) {
    return this.call<{ state: string; lease_expired: boolean }>("POST", `/runs/${runId}/heartbeat`);
  }

  detection(runId: string, kind: string, detail: Record<string, unknown>) {
    return this.call<StepResult>("POST", `/runs/${runId}/detections`, { kind, detail });
  }

  final(runId: string, observation: ObservationV1) {
    return this.call<StepResult>("POST", `/runs/${runId}/final`, { observation });
  }

  stop(runId: string, reason: string, detail: Record<string, unknown>) {
    return this.call<StepResult>("POST", `/runs/${runId}/stop`, { reason, detail });
  }

  // The existing exact-document endpoint (session- and pack-pinned). The
  // executor verifies these bytes against the approved SHA before use.
  async document(kind: string): Promise<LocalDocument> {
    const response = await this.fetchImpl(
      `${this.baseUrl}/api/handoff/sessions/${this.sessionId}/documents/${kind}`,
      { headers: { "X-Handoff-Session-Token": this.sessionToken } });
    if (!response.ok) throw new FillServerError(response.status, `document ${kind}`);
    const disposition = response.headers.get("content-disposition") ?? "";
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    return {
      bytes: new Uint8Array(await response.arrayBuffer()),
      filename: match ? decodeURIComponent(match[1]) : kind,
      mediaType: response.headers.get("content-type") ?? "application/octet-stream",
      sha256: response.headers.get("x-content-hash") ?? "",
    };
  }
}
