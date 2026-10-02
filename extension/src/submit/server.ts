// Bundle 6E-A typed client for the extension submit routes (spec §18), under
// the same handoff session token as the 6D-B fill routes.
import type { ObservationV1 } from "../fill/observation-types";
import { FILL_SERVER_BASE_URL } from "../fill/server";
import { authHeaders } from "../shared/backend";

export class SubmitServerError extends Error {
  constructor(readonly status: number, readonly reason: string) {
    super(`${status}: ${reason}`);
  }
}

export interface PreClickBody {
  grant_id: string;
  observation: ObservationV1;
  verification: Record<string, unknown>;
}

export interface SubmitServer {
  observation(runId: string, phase: "REVIEW" | "CHALLENGE_CLEARED" | "POST_SUBMIT", observation: ObservationV1,
              attemptId?: string | null): Promise<unknown>;
  preClick(runId: string, body: PreClickBody): Promise<{ attempt_id: string }>;
  dispatch(runId: string, attemptId: string): Promise<{ dispatched: boolean }>;
  event(runId: string, attemptId: string, event: string, detail: Record<string, unknown>): Promise<unknown>;
  result(runId: string, attemptId: string, evidence: Record<string, unknown>)
    : Promise<{ state: string; reason: string | null }>;
  cancel(runId: string, attemptId: string): Promise<unknown>;
}

type Fetch = typeof fetch;

export class HttpSubmitServer implements SubmitServer {
  constructor(private readonly sessionId: string, private readonly sessionToken: string,
              private readonly fetchImpl: Fetch = (...args) => fetch(...args),
              private readonly baseUrl: string = FILL_SERVER_BASE_URL) {}

  private async call<T>(path: string, body?: unknown): Promise<T> {
    const response = await this.fetchImpl(`${this.baseUrl}/api/handoff/sessions/${this.sessionId}/fill${path}`, {
      method: "POST", headers: { ...(await authHeaders()), "X-Handoff-Session-Token": this.sessionToken,
                                 "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail ?? null);
      throw new SubmitServerError(response.status, detail);
    }
    return payload as T;
  }

  observation(runId: string, phase: "REVIEW" | "CHALLENGE_CLEARED" | "POST_SUBMIT", observation: ObservationV1,
              attemptId: string | null = null) {
    return this.call(`/runs/${runId}/submit/observations`, { phase, attempt_id: attemptId, observation });
  }

  preClick(runId: string, body: PreClickBody) {
    return this.call<{ attempt_id: string }>(`/runs/${runId}/submit/pre-click`, body);
  }

  dispatch(runId: string, attemptId: string) {
    return this.call<{ dispatched: boolean }>(`/runs/${runId}/submit/${attemptId}/dispatch`);
  }

  event(runId: string, attemptId: string, event: string, detail: Record<string, unknown>) {
    return this.call(`/runs/${runId}/submit/${attemptId}/events`, { event, detail });
  }

  result(runId: string, attemptId: string, evidence: Record<string, unknown>) {
    return this.call<{ state: string; reason: string | null }>(`/runs/${runId}/submit/${attemptId}/result`,
                                                              { evidence });
  }

  cancel(runId: string, attemptId: string) {
    return this.call(`/runs/${runId}/submit/${attemptId}/cancel`);
  }
}
