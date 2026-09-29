// Bundle 6E-A SubmitController (spec §10, §12, J3-J5). It acts only on a
// human SUBMIT authorization delivered by the run heartbeat, and only on the
// live, quarantined tab of the FILLED run it belongs to:
//   1. local proofs (detections, TOTAL read-back, siblings, the bound control)
//   2. PRE_SUBMIT observation (+ a visible challenge refuses)
//   3. server pre-click (the server re-proves the exact review)
//   4. cancel window
//   5. DISPATCHING persisted -> server CLICK_DISPATCHED ack -> DISPATCHED_ACK
//   6. certified SUBMIT egress installed and verified by exact read-back
//   7. CLICKED persisted -> the one-shot submit allowance -> SUBMIT_CLICK
//   8. bounded watch for certified signals; a challenge is handed to the
//      human (never touched); an edit or a changed re-observation restores
//      TOTAL immediately
//   9. TOTAL restored and verified -> matched-rule evidence -> the server
//      decides the result.
// A restarted worker never clicks again: recoverSubmitAfterRestart restores
// TOTAL and reports what it knows.

import type { ObservationV1 } from "../fill/observation-types";
import type { PhaseStore } from "../fill/run-controller";
import type { ResolvedEgress } from "./certification";
import {
  CHALLENGE_HANDOFF_WINDOW_MS, MATCHED_ALLOW_RULES_REPORTED, SUBMIT_RESULT_POLL_MS, SUBMIT_RESULT_WINDOW_MS,
} from "./constants";
import { observationFingerprint } from "./fingerprint";
import { SubmitServerError, type SubmitServer } from "./server";
import type { Signals } from "./signals";

export interface AuthorizationDirective {
  authorization_id: string;
  grant_id: string;
  review_hash: string;
  expires_at: string;
  certification_id: string;
  egress: ResolvedEgress[];
  confirmation_url: string;
  e1_rule_id: number;
  expected: { canonical_url: string; observation_fingerprint: string; submit_control_fingerprint: string;
              ruleset_hash: string };
}

export interface SubmitPagePort {
  observe(): Promise<ObservationV1>;
  detected(): Promise<string[]>;
  watchContent(): Promise<void>;
  contentChanged(): Promise<boolean>;
  findSubmitControl(certificationId: string, fingerprint: string): Promise<boolean>;
  clickSubmit(certificationId: string, fingerprint: string): Promise<"CLICKED" | "SUBMIT_CONTROL_MISSING">;
  signals(certificationId: string, context: { boundUrl: string; confirmationUrl: string })
    : Promise<Signals & { rootPresent: boolean }>;
}

export interface SubmitEgressPort {
  verifyTotal(totalHash: string): Promise<boolean>;
  install(egress: ResolvedEgress[]): Promise<string>;
  verify(egressHash: string): Promise<boolean>;
  restoreTotal(totalHash: string): Promise<boolean>;
  matched(sinceMs: number): Promise<{ available: boolean; ids: number[] }>;
}

export interface SubmitBrowserPort {
  siblingsContained(): Promise<boolean>;
  now(): number;
  sleep(ms: number): Promise<void>;
}

export interface SubmitPorts {
  server: SubmitServer;
  page: SubmitPagePort;
  egress: SubmitEgressPort;
  browser: SubmitBrowserPort;
  store: PhaseStore;
  timing?: { pollMs?: number; windowMs?: number; challengeMs?: number };
}

export interface SubmitContext {
  tabId: number;
  runId: string;
  sessionId: string;
  sessionToken: string;
  executorInstanceId: string;
  browserSessionId: string;
}

export type SubmitPhase = "REVIEW_OBSERVING" | "AUTHORIZED" | "DISPATCHING" | "SUBMITTING" | "CHALLENGE"
  | "SUBMITTED" | "UNCLEAR" | "NOT_SUBMITTED";

export interface SubmitView { phase: SubmitPhase; reason: string | null; attemptId: string | null }

export const SUBMIT_KEY_PREFIX = "submit:";

export interface StoredSubmit {
  tabId: number;
  runId: string;
  sessionId: string;
  sessionToken: string;
  attemptId: string;
  phase: "DISPATCHING" | "DISPATCHED_ACK" | "CLICKED";
  dispatchedAt: number;
  totalHash: string;
}

interface WatchOutcome { success: boolean; failure: boolean; content: boolean; cause: string | null }

const DETECTIONS_THAT_REFUSE = new Set(["SUBMIT_ATTEMPT_OBSERVED", "NAVIGATION_ATTEMPT_OBSERVED",
  "POST_FILL_CHANGE_OBSERVED"]);

const RESULT_PHASE: Record<string, SubmitPhase> = {
  CONFIRMED_SUCCESS: "SUBMITTED", SUBMISSION_FAILED: "NOT_SUBMITTED", SUBMISSION_AMBIGUOUS: "UNCLEAR",
};

function reasonOf(error: unknown): string {
  return error instanceof SubmitServerError ? error.reason : "SERVER_UNREACHABLE";
}

export class SubmitController {
  view: SubmitView | null = null;
  private readonly handledGrants = new Set<string>();
  private readonly answeredRequests = new Set<string>();
  private busy = false;
  private cancelRequested = false;
  private dispatchStarted = false;

  constructor(private readonly ports: SubmitPorts, private readonly ctx: SubmitContext,
              private readonly onView: (view: SubmitView) => void = () => undefined) {}

  private set(phase: SubmitPhase, reason: string | null, attemptId: string | null = this.view?.attemptId ?? null)
    : SubmitView {
    this.view = { phase, reason, attemptId };
    this.onView(this.view);
    return this.view;
  }

  private get key(): string {
    return SUBMIT_KEY_PREFIX + this.ctx.tabId;
  }

  // The Submit Review asked for a fresh look at the page (spec §7, E12).
  async observeForReview(requestId: string): Promise<void> {
    if (this.busy || this.answeredRequests.has(requestId)) return;
    try {
      await this.ports.server.observation(this.ctx.runId, "REVIEW", await this.ports.page.observe());
      this.answeredRequests.add(requestId);
    } catch {
      // the next heartbeat still carries the request; nothing to undo
    }
  }

  // Only before DISPATCHING is persisted (spec E14).
  cancel(): boolean {
    if (!this.busy || this.dispatchStarted) return false;
    this.cancelRequested = true;
    return true;
  }

  async run(auth: AuthorizationDirective): Promise<SubmitView> {
    if (this.busy || this.handledGrants.has(auth.grant_id)) return this.view ?? this.set("AUTHORIZED", null);
    this.busy = true;
    this.handledGrants.add(auth.grant_id);
    this.cancelRequested = false;
    this.dispatchStarted = false;
    try {
      return await this.steps(auth);
    } finally {
      this.busy = false;
    }
  }

  private async event(attemptId: string, event: string, detail: Record<string, unknown> = {}): Promise<void> {
    await this.ports.server.event(this.ctx.runId, attemptId, event, detail).catch(() => undefined);
  }

  private async steps(auth: AuthorizationDirective): Promise<SubmitView> {
    const { server, page, egress, browser, store } = this.ports;
    this.set("AUTHORIZED", null, null);
    await page.watchContent().catch(() => undefined);
    // 1. local proofs
    let local: string | null = null;
    if ((await page.detected()).some((kind) => DETECTIONS_THAT_REFUSE.has(kind))) local = "DETECTION_OBSERVED";
    else if (!(await egress.verifyTotal(auth.expected.ruleset_hash))) local = "QUARANTINE_NOT_VERIFIED";
    else if (!(await browser.siblingsContained())) local = "SIBLING_EMPLOYER_CONTEXT_OPEN";
    else if (!(await page.findSubmitControl(auth.certification_id, auth.expected.submit_control_fingerprint))) {
      local = "SUBMIT_CONTROL_MISSING";
    }
    // 2. PRE_SUBMIT observation
    const observation = await page.observe();
    const signals = await page.signals(auth.certification_id, this.signalContext(auth))
      .catch(() => ({ challenge: false }) as Signals & { rootPresent: boolean });
    const verification: Record<string, unknown> = {
      executor_instance_id: this.ctx.executorInstanceId, browser_session_id: this.ctx.browserSessionId,
      execution_tab_id: this.ctx.tabId, canonical_url: observation.context.canonical_url,
      observation_fingerprint: await observationFingerprint(observation),
      submit_control_fingerprint: local === "SUBMIT_CONTROL_MISSING" ? "" : auth.expected.submit_control_fingerprint,
      ruleset_hash: local === "QUARANTINE_NOT_VERIFIED" ? "" : auth.expected.ruleset_hash,
      challenge_visible: signals.challenge,
      ...(local ? { local_refusal: local } : {}),
    };
    // 3. pre-click: the server re-proves the exact review and claims the intent
    let attemptId: string;
    try {
      attemptId = (await server.preClick(this.ctx.runId, { grant_id: auth.grant_id, observation, verification }))
        .attempt_id;
    } catch (error) {
      return this.set("NOT_SUBMITTED", reasonOf(error));
    }
    this.set("AUTHORIZED", null, attemptId);
    // 4. the cancel window closes here
    if (this.cancelRequested) {
      await server.cancel(this.ctx.runId, attemptId).catch(() => undefined);
      return this.set("NOT_SUBMITTED", "CANCELLED");
    }
    // 5. durable dispatch before anything physical (J3)
    this.dispatchStarted = true;
    const record: StoredSubmit = { tabId: this.ctx.tabId, runId: this.ctx.runId, sessionId: this.ctx.sessionId,
      sessionToken: this.ctx.sessionToken, attemptId, phase: "DISPATCHING", dispatchedAt: browser.now(),
      totalHash: auth.expected.ruleset_hash };
    await store.set(this.key, record);
    this.set("DISPATCHING", null);
    const dispatched = await server.dispatch(this.ctx.runId, attemptId).then((r) => r.dispatched, () => false);
    if (!dispatched) {
      await store.remove(this.key);
      return this.set("NOT_SUBMITTED", "DISPATCH_REFUSED");
    }
    await store.set(this.key, { ...record, phase: "DISPATCHED_ACK" });
    this.set("SUBMITTING", null);
    // 6. the certified egress, verified by exact read-back before the click
    const egressHash = await egress.install(auth.egress).catch(() => null);
    await this.event(attemptId, "EGRESS_INSTALLED", { ruleset_hash: egressHash });
    if (egressHash === null || !(await egress.verify(egressHash))) {
      await this.event(attemptId, "EGRESS_VERIFY_FAILED");
      return this.finish(auth, record, { success: false, failure: false, content: false,
                                         cause: "EGRESS_NOT_VERIFIED" }, false);
    }
    // 7. the one click
    await store.set(this.key, { ...record, phase: "CLICKED" });
    const clicked = await page.clickSubmit(auth.certification_id, auth.expected.submit_control_fingerprint)
      .catch(() => "SUBMIT_CONTROL_MISSING" as const);
    if (clicked !== "CLICKED") {
      await this.event(attemptId, "SUBMIT_CONTROL_MISSING");
      return this.finish(auth, record, { success: false, failure: false, content: false,
                                         cause: "SUBMIT_CONTROL_MISSING" }, false);
    }
    await this.event(attemptId, "CLICK_PERFORMED");
    // 8. bounded watch
    return this.finish(auth, record, await this.watch(auth, attemptId, egressHash), true);
  }

  private signalContext(auth: AuthorizationDirective) {
    return { boundUrl: auth.expected.canonical_url, confirmationUrl: auth.confirmation_url };
  }

  private async contentChangedStop(auth: AuthorizationDirective, attemptId: string): Promise<WatchOutcome> {
    await this.event(attemptId, "CONTENT_CHANGED_DURING_ATTEMPT");
    // Restore TOTAL immediately; finish() re-verifies it.
    await this.ports.egress.restoreTotal(auth.expected.ruleset_hash).catch(() => false);
    return { success: false, failure: false, content: true, cause: "CONTENT_CHANGED" };
  }

  private async watch(auth: AuthorizationDirective, attemptId: string, egressHash: string): Promise<WatchOutcome> {
    const { page, browser } = this.ports;
    const pollMs = this.ports.timing?.pollMs ?? SUBMIT_RESULT_POLL_MS;
    let deadline = browser.now() + (this.ports.timing?.windowMs ?? SUBMIT_RESULT_WINDOW_MS);
    let challenge = false;
    let everChallenged = false;
    while (browser.now() < deadline) {
      await browser.sleep(pollMs);
      if (await page.contentChanged().catch(() => false)) return this.contentChangedStop(auth, attemptId);
      let signals: Signals & { rootPresent: boolean };
      try {
        signals = await page.signals(auth.certification_id, this.signalContext(auth));
      } catch {
        continue;  // mid-navigation: look again on the next poll
      }
      // A cleared challenge is revalidated FIRST, before any success or
      // failure on the same poll is accepted (the page's own callback may
      // submit the moment the person completes the check).
      if (challenge && !signals.challenge) {
        challenge = false;
        await this.event(attemptId, "CHALLENGE_CLEARED");
        this.set("SUBMITTING", null);
        // A full fresh revalidation before continuing (spec §12.2): the
        // SUBMIT egress is still exactly the certified one, and the form,
        // if still there, is exactly the authorized one -- by the local
        // fingerprint AND the server's comparison. If the form is already
        // gone, the trusted-edit watcher (polled above for the whole
        // handoff) is what vouches for the content. Anything else, or an
        // unreachable server, restores TOTAL and stops.
        if (!(await this.ports.egress.verify(egressHash).catch(() => false))) {
          await this.event(attemptId, "EGRESS_VERIFY_FAILED", { stage: "challenge_cleared" });
          await this.ports.egress.restoreTotal(auth.expected.ruleset_hash).catch(() => false);
          return { success: false, failure: false, content: false, cause: "QUARANTINE_CHANGED" };
        }
        if (signals.rootPresent) {
          const observation = await page.observe().catch(() => null);
          const local = observation !== null
            && await observationFingerprint(observation) === auth.expected.observation_fingerprint;
          const server = observation !== null && await this.ports.server
            .observation(this.ctx.runId, "CHALLENGE_CLEARED", observation, attemptId)
            .then((r) => (r as { matches_review?: unknown }).matches_review === true, () => false);
          if (!local || !server) return this.contentChangedStop(auth, attemptId);
        }
      }
      if (signals.success) {
        await this.event(attemptId, "SIGNAL_OBSERVED", { signal: "success" });
        return { success: true, failure: false, content: false, cause: null };
      }
      if (signals.failure) {
        await this.event(attemptId, "SIGNAL_OBSERVED", { signal: "failure" });
        return { success: false, failure: true, content: false, cause: null };
      }
      if (signals.challenge && !challenge) {
        challenge = true;
        everChallenged = true;
        this.set("CHALLENGE", null);
        await this.event(attemptId, "CHALLENGE_DETECTED");
        deadline = browser.now() + (this.ports.timing?.challengeMs ?? CHALLENGE_HANDOFF_WINDOW_MS);
      }
    }
    return { success: false, failure: false, content: false, cause: everChallenged ? "CHALLENGE_TIMEOUT" : "NO_SIGNAL" };
  }

  private async finish(auth: AuthorizationDirective, record: StoredSubmit, outcome: WatchOutcome,
                       clickPerformed: boolean): Promise<SubmitView> {
    const { server, page, egress, store } = this.ports;
    const restored = await egress.restoreTotal(auth.expected.ruleset_hash).catch(() => false);
    await this.event(record.attemptId, restored ? "TOTAL_RESTORED" : "TOTAL_RESTORE_FAILED");
    const matched = await egress.matched(record.dispatchedAt).catch(() => ({ available: false, ids: [] as number[] }));
    try {
      await server.observation(this.ctx.runId, "POST_SUBMIT", await page.observe(), record.attemptId);
    } catch {
      // the form may be gone (a confirmation page); the observation is best effort
    }
    const evidence = {
      click_performed: clickPerformed, egress_ever_installed: true, total_restored_verified: restored,
      success_observed: outcome.success, failure_observed: outcome.failure, content_changed: outcome.content,
      matched_rule_ids: matched.ids, matched_rules_available: matched.available && MATCHED_ALLOW_RULES_REPORTED,
      cause: outcome.cause,
    };
    let state = "SUBMISSION_AMBIGUOUS";
    let reason: string | null = outcome.cause;
    try {
      const result = await server.result(this.ctx.runId, record.attemptId, evidence);
      state = result.state;
      reason = result.reason ?? outcome.cause;
    } catch (error) {
      reason = reasonOf(error);  // the server sweep will mark it ambiguous
    }
    await store.remove(this.key);
    return this.set(RESULT_PHASE[state] ?? "UNCLEAR", reason);
  }
}

// After a worker restart (spec J3): never click; restore TOTAL; report.
export async function recoverSubmitAfterRestart(store: PhaseStore,
                                                makeServer: (sessionId: string, token: string) => SubmitServer,
                                                egressFor: (tabId: number) => SubmitEgressPort): Promise<void> {
  for (const key of (await store.keys()).filter((k) => k.startsWith(SUBMIT_KEY_PREFIX))) {
    const record = await store.get<StoredSubmit>(key);
    if (!record) continue;
    const server = makeServer(record.sessionId, record.sessionToken);
    const egress = egressFor(record.tabId);
    const restored = await egress.restoreTotal(record.totalHash).catch(() => false);
    const matched = await egress.matched(record.dispatchedAt).catch(() => ({ available: false, ids: [] as number[] }));
    await server.event(record.runId, record.attemptId, "EXECUTOR_RESTARTED", { phase: record.phase })
      .catch(() => undefined);
    await server.result(record.runId, record.attemptId, {
      click_performed: record.phase === "CLICKED" ? "UNKNOWN" : false,
      egress_ever_installed: record.phase !== "DISPATCHING", total_restored_verified: restored,
      success_observed: false, failure_observed: false, content_changed: false,
      matched_rule_ids: matched.ids, matched_rules_available: matched.available && MATCHED_ALLOW_RULES_REPORTED,
      cause: "EXECUTOR_RESTARTED",
    }).catch(() => undefined);
    await store.remove(key);
  }
}
