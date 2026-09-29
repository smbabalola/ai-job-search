// Bundle 6D-B run controller (spec §10.4, §11.1, §11.3, §11.4). A pure state
// machine over injected ports (server, page, quarantine, browser, phase
// store), wired to chrome.* in background/index.ts. The server is the
// authority for every transition; the controller only reports observational
// evidence, executes one enveloped action at a time and stops on anything
// unexpected. Nothing is ever retried or resumed: a write whose outcome was
// not reported is WRITE_OUTCOME_UNKNOWN, and a new run starts from scratch.

import { HEARTBEAT_INTERVAL_MS, REVALIDATION_MOUNT_TIMEOUT_MS, SETTLE_CAP_MS } from "./constants";
import type { ActionOutcome, Envelope, PlanAction } from "./executor";
import type { ObservationV1 } from "./observation-types";
import { structureFingerprint } from "./observer";
import { FillServerError, type FillServer, type LocalDocument, type PlanStatusAction } from "./server";

export type Phase = "OBSERVING" | "NEEDS_REVIEW" | "UNSUPPORTED" | "PREPARING" | "FILLING" | "FILLED" | "STOPPED";

export interface ControllerView {
  phase: Phase;
  runId: string | null;
  reason: string | null;
  detail: Record<string, unknown>;
  progress: { done: number; total: number } | null;
}

export interface PagePort {
  rootPresent(): Promise<boolean>;
  observe(): Promise<ObservationV1>;
  execute(action: PlanAction, envelope: Envelope | null, attachment: LocalDocument | null): Promise<ActionOutcome>;
  installDetections(runId: string): Promise<void>;
  detected(): Promise<string[]>;
  enablePostFill(): Promise<void>;
}

export interface QuarantinePort {
  // The tab another fill ruleset is installed for, or null (6D-B: one
  // quarantined tab per browser, the rule ids are fixed).
  otherQuarantinedTab(tabId: number): Promise<number | null>;
  install(kind: "PRELOAD" | "TOTAL"): Promise<string>;
  verify(rulesetHash: string): Promise<boolean>;
  remove(): Promise<void>;
}

export interface BrowserPort {
  permissionsGranted(): Promise<boolean>;
  siblingsContained(): Promise<boolean>;
  reloadAndWait(): Promise<void>;
  sleep(ms: number): Promise<void>;
  setBadge(text: string): Promise<void>;
}

export interface PhaseStore {
  get<T>(key: string): Promise<T | undefined>;
  set(key: string, value: unknown): Promise<void>;
  remove(key: string): Promise<void>;
  keys(): Promise<string[]>;
}

export interface Timers {
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

export interface FillPorts {
  server: FillServer;
  page: PagePort;
  quarantine: QuarantinePort;
  browser: BrowserPort;
  store: PhaseStore;
  timers?: Timers;
  timing?: { heartbeatMs?: number; mountTimeoutMs?: number; pollMs?: number };
}

export interface RunContext {
  tabId: number;
  sessionId: string;
  sessionToken: string;
  executorInstanceId: string;
  browserSessionId: string;
}

export const RUN_KEY_PREFIX = "fill:run:";
export const ACTION_KEY_PREFIX = "fill:action:";
const MISMATCH_HASH = "sha256:" + "0".repeat(64);

export interface StoredRun { runId: string; tabId: number; sessionId: string; sessionToken: string }
export interface StoredAction extends StoredRun { index: number; envelopeId: string | null; phase: "MAY_HAVE_WRITTEN" }

class RunEnded extends Error {
  constructor(readonly view: ControllerView) {
    super(view.phase);
  }
}

const TERMINAL: Record<string, Phase> = {
  PLAN_NEEDS_REVIEW: "NEEDS_REVIEW", UNSUPPORTED_FORM: "UNSUPPORTED", FILL_STOPPED: "STOPPED",
  FILLED_AWAITING_SUBMISSION: "FILLED", FILLED_CONTEXT_UNVERIFIED: "STOPPED",
};

// A new run may start in a tab only if no quarantine is installed for it
// and no earlier run there got past planning. Replacing a tab's TOTAL rules
// with PRELOAD would let the still-filled page send GETs before the reset
// reload; so a quarantined or filled tab is never re-run in place: the only
// exit is closing it and opening the job page fresh (spec §10.7, §16.4).
export function mayStartOnTab(previousPhase: Phase | null, tabHasFillRules: boolean): boolean {
  if (tabHasFillRules) return false;
  return previousPhase === null || previousPhase === "NEEDS_REVIEW" || previousPhase === "UNSUPPORTED"
    || previousPhase === "STOPPED";
}

// The spec §11.3 a value-state expectations, checked locally before every
// intent (the server re-checks the same PRE_ACTION observation).
export function localValueProblem(obs: ObservationV1, actions: PlanStatusAction[], completed: Set<number>,
                                  next: number): { reason: string; page_field_key: string } | null {
  const byKey = new Map(obs.elements.filter((e) => e.classification === "APPLICATION")
    .map((e) => [e.page_field_key, e] as const));
  for (const [index, action] of actions.entries()) {
    const element = byKey.get(action.page_field_key);
    if (!element) continue;  // structure is the server's diff
    const state = element.value_state;
    const current = state.state === "NONBLANK" ? state.current_value_hash : null;
    if (action.action_kind === "OMIT" && current !== null) {
      return { reason: "OMIT_FIELD_NOT_BLANK", page_field_key: action.page_field_key };
    }
    if (action.action_kind !== "WRITE" && action.action_kind !== "ATTACH_LOCAL") continue;
    if (completed.has(index) && current !== action.rendered_value_hash) {
      return { reason: "FIELD_VALUE_REVERTED", page_field_key: action.page_field_key };
    }
    if (!completed.has(index) && index >= next && current !== null && current !== action.rendered_value_hash) {
      return { reason: "PREFILLED_VALUE_CONFLICT", page_field_key: action.page_field_key };
    }
  }
  return null;
}

export class FillRunController {
  view: ControllerView = { phase: "OBSERVING", runId: null, reason: null, detail: {}, progress: null };
  private heartbeat: unknown = null;
  // Set when a detection or a closed tab ended the run server-side: the
  // controller stops at the next step and never touches the DOM again.
  private aborted: ControllerView | null = null;
  private readonly forwarded = new Set<string>();
  private readonly timers: Timers;

  constructor(private readonly ports: FillPorts, private readonly context: RunContext,
              private readonly onChange: (view: ControllerView) => void = () => {}) {
    this.timers = ports.timers ?? { setInterval: (fn, ms) => setInterval(fn, ms),
      clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>) };
  }

  private set(update: Partial<ControllerView>): void {
    if (this.aborted) return;
    this.view = { ...this.view, ...update };
    this.onChange(this.view);
  }

  private end(phase: Phase, reason: string | null, detail: Record<string, unknown> = {}): never {
    this.set({ phase, reason, detail });
    throw new RunEnded(this.view);
  }

  // Maps a server step response; ends the run on a terminal state.
  private follow(result: { state: string; reason?: string | null; [k: string]: unknown }): void {
    const phase = TERMINAL[result.state];
    if (phase) this.end(phase, (result.reason as string | null | undefined) ?? null, result);
  }

  private async stop(reason: string, detail: Record<string, unknown> = {}): Promise<never> {
    await this.ports.server.stop(this.view.runId!, reason, detail);
    return this.end("STOPPED", reason, detail);
  }

  private startHeartbeat(): void {
    const ms = this.ports.timing?.heartbeatMs ?? HEARTBEAT_INTERVAL_MS;
    this.heartbeat = this.timers.setInterval(() => {
      void this.ports.server.heartbeat(this.view.runId!).then((beat) => {
        if (beat.lease_expired) this.stopHeartbeat();
      }).catch(() => this.stopHeartbeat());
    }, ms);
  }

  stopHeartbeat(): void {
    if (this.heartbeat !== null) this.timers.clearInterval(this.heartbeat);
    this.heartbeat = null;
  }

  async run(): Promise<ControllerView> {
    const { server, store } = this.ports;
    try {
      const run = await server.startRun({ executor_instance_id: this.context.executorInstanceId,
        browser_session_id: this.context.browserSessionId, execution_tab_id: this.context.tabId });
      this.set({ runId: run.id, phase: "OBSERVING" });
      await store.set(RUN_KEY_PREFIX + this.context.tabId, { runId: run.id, tabId: this.context.tabId,
        sessionId: this.context.sessionId, sessionToken: this.context.sessionToken } satisfies StoredRun);
      this.startHeartbeat();
      await this.steps(run.id);
    } catch (error) {
      if (!(error instanceof RunEnded)) await this.fail(error);
    }
    if (this.view.phase === "FILLED") {
      // The quarantined page stays observed; the lease stays alive (§12.4).
      await this.ports.page.enablePostFill().catch(() => undefined);
    } else {
      this.stopHeartbeat();
      await store.remove(RUN_KEY_PREFIX + this.context.tabId);
    }
    return this.view;
  }

  // An unexpected failure: a server refusal ends the run as the server
  // recorded it; anything local stops the run (never resumed).
  private async fail(error: unknown): Promise<void> {
    if (this.view.runId === null) {
      this.set({ phase: "STOPPED", reason: error instanceof FillServerError ? error.detail : "START_FAILED" });
      return;
    }
    try {
      const status = await this.ports.server.planStatus(this.view.runId);
      const phase = TERMINAL[status.state];
      if (phase) {
        this.set({ phase, reason: status.reason });
        return;
      }
      await this.ports.server.stop(this.view.runId, "EXECUTOR_LOST", { error: String(error).slice(0, 200) });
    } catch {
      // The lease expiry records EXECUTOR_LOST server-side.
    }
    this.set({ phase: "STOPPED", reason: "EXECUTOR_LOST" });
  }

  private async steps(runId: string): Promise<void> {
    const { server, page, quarantine, browser } = this.ports;
    // 1-2. permissions and sibling containment
    if (!await browser.permissionsGranted()) await this.stop("PERMISSIONS_MISSING");
    if (!await browser.siblingsContained()) await this.stop("SIBLING_EMPLOYER_CONTEXT_OPEN");
    const other = await quarantine.otherQuarantinedTab(this.context.tabId);
    if (other !== null) await this.stop("QUARANTINE_RULESET_CHANGED", { cause: "another_quarantined_tab", tab: other });
    // 3. INITIAL observation: the server plans (or ends the run for review)
    this.follow(await server.observation(runId, "INITIAL", await page.observe()));
    // 4. the communications reset
    this.set({ phase: "PREPARING" });
    const preload = await quarantine.install("PRELOAD");
    if (!await quarantine.verify(preload)) {
      this.follow(await server.quarantine(runId, "LOST", null));
    }
    await server.quarantine(runId, "PRELOAD_INSTALLED", preload);
    await browser.reloadAndWait();
    // After the reload: Chrome clears a tab's badge on navigation.
    await browser.setBadge("Q");
    await server.quarantine(runId, "RELOADED", preload);
    await this.waitForRoot();
    const revalidation = await server.observation(runId, "REVALIDATION", await page.observe());
    this.follow(revalidation);
    await page.installDetections(runId);
    const total = await quarantine.install("TOTAL");
    if (!await quarantine.verify(total)) this.follow(await server.quarantine(runId, "LOST", null));
    this.follow(await server.quarantine(runId, "TOTAL_VERIFIED", total));
    // 5. the grant
    const grant = await server.grant(runId);
    if (!grant.granted) this.end("STOPPED", grant.stop_reason);
    // 6. every action, strictly in plan order
    const status = await server.planStatus(runId);
    const actions = status.actions;
    const completed = new Set<number>();
    this.set({ phase: "FILLING", progress: { done: 0, total: actions.length } });
    for (const [index, action] of actions.entries()) {
      await this.act(runId, index, action, actions, completed, total);
      completed.add(index);
      this.set({ progress: { done: index + 1, total: actions.length } });
    }
    // 7. final validation
    await this.pollDetections();
    this.follow(await server.final(runId, await page.observe()));
    this.end("STOPPED", "UNEXPECTED_FINAL_STATE");
  }

  private async waitForRoot(): Promise<void> {
    const { page, browser } = this.ports;
    const timeout = this.ports.timing?.mountTimeoutMs ?? REVALIDATION_MOUNT_TIMEOUT_MS;
    const poll = this.ports.timing?.pollMs ?? SETTLE_CAP_MS;
    // The form may mount late (SPA): wait for the adapter's root, bounded.
    // If it never mounts, the REVALIDATION observation reports it and the
    // server ends the run as UNSUPPORTED_FORM(RESET_SURFACE_MISMATCH).
    for (let waited = 0; waited < timeout; waited += poll) {
      if (await page.rootPresent()) return;
      await browser.sleep(poll);
    }
  }

  private checkAborted(): void {
    if (this.aborted) throw new RunEnded(this.aborted);
  }

  // Detections the page recorded but whose message may not have arrived yet.
  private async pollDetections(): Promise<void> {
    for (const kind of await this.ports.page.detected()) await this.onDetection(kind, { via: "poll" });
    this.checkAborted();
  }

  private async act(runId: string, index: number, action: PlanStatusAction, actions: PlanStatusAction[],
                    completed: Set<number>, rulesetHash: string): Promise<void> {
    const { server, page, quarantine, browser, store } = this.ports;
    await this.pollDetections();
    // a. local pre-check
    const contained = await browser.siblingsContained();
    const rulesetOk = await quarantine.verify(rulesetHash);
    const observed = await page.observe();
    const problem = localValueProblem(observed, actions, completed, index);
    if (problem) await this.stop(problem.reason, { page_field_key: problem.page_field_key });
    this.follow(await server.observation(runId, "PRE_ACTION", observed, index));
    const target = observed.elements.find((e) => e.page_field_key === action.page_field_key);
    const precheck = { ruleset_hash: rulesetOk ? rulesetHash : MISMATCH_HASH,
      structure_fingerprint: await structureFingerprint(observed),
      field_fingerprint: target?.field_fingerprint ?? MISMATCH_HASH, siblings_contained: contained };
    // b. intent
    const intent = await server.intent(runId, index, precheck);
    if (!intent.envelope) this.end("STOPPED", intent.stop_reason, intent.detail);
    const envelope = intent.envelope!;
    const attachment = action.action_kind === "ATTACH_LOCAL" ? await server.document(action.document_kind!) : null;
    // c. persist MAY_HAVE_WRITTEN before touching the DOM
    const key = ACTION_KEY_PREFIX + runId;
    const writes = action.action_kind === "WRITE" || action.action_kind === "ATTACH_LOCAL";
    if (writes) {
      await store.set(key, { runId, tabId: this.context.tabId, sessionId: this.context.sessionId,
        sessionToken: this.context.sessionToken, index, envelopeId: envelope.envelope_id,
        phase: "MAY_HAVE_WRITTEN" } satisfies StoredAction);
    }
    let outcome: ActionOutcome;
    let post: ObservationV1;
    this.checkAborted();  // nothing written yet: a stop now means no DOM call at all
    try {
      outcome = await page.execute(action as unknown as PlanAction, envelope, attachment);
      post = await page.observe();
    } catch (error) {
      if (writes) {
        this.follow(await server.unknown(runId, index));  // never retried
      }
      throw error;
    }
    // d. the outcome (at most one per action, server-side)
    const result = await server.outcome(runId, index, { envelope_id: envelope.envelope_id, outcome: outcome.outcome,
      readback_hash: outcome.readbackHash ?? null, post_observation: post });
    if (writes) await store.remove(key);
    this.follow(result);
  }

  // The execution tab closed: recorded, and the quarantine rules go with it.
  async onTabClosed(): Promise<void> {
    this.stopHeartbeat();
    if (this.view.phase !== "FILLED") this.abort("STOPPED", "EXECUTION_CONTEXT_CLOSED");
    if (this.view.runId) {
      await this.ports.server.detection(this.view.runId, "EXECUTION_CONTEXT_CLOSED", {}).catch(() => undefined);
    }
    await this.ports.quarantine.remove();
    await this.ports.store.remove(RUN_KEY_PREFIX + this.context.tabId);
  }

  private abort(phase: Phase, reason: string | null): void {
    this.set({ phase, reason });
    this.aborted = this.view;
  }

  async onDetection(kind: string, detail: Record<string, unknown>): Promise<void> {
    if (!this.view.runId || this.forwarded.has(kind)) return;
    this.forwarded.add(kind);
    const result = await this.ports.server.detection(this.view.runId, kind, detail).catch(() => null);
    // Evidence is always forwarded; only a run still in progress can be ended
    // by it. A run that already ended keeps the reason it ended with (the
    // server answers an ended run with its state only).
    const inProgress = this.view.phase === "OBSERVING" || this.view.phase === "PREPARING"
      || this.view.phase === "FILLING";
    if (result && TERMINAL[result.state] && inProgress) {
      this.abort(TERMINAL[result.state], (result.reason as string | null) ?? null);
    }
  }
}

// On service-worker start: a run is never resumed. A stored
// MAY_HAVE_WRITTEN action becomes WRITE_OUTCOME_UNKNOWN; any other run the
// worker was driving stops as EXECUTOR_LOST.
export async function recoverAfterRestart(store: PhaseStore,
                                          serverFor: (sessionId: string, token: string) => FillServer): Promise<string[]> {
  const handled: string[] = [];
  const keys = await store.keys();
  for (const key of keys.filter((k) => k.startsWith(ACTION_KEY_PREFIX))) {
    const action = await store.get<StoredAction>(key);
    if (action?.phase === "MAY_HAVE_WRITTEN") {
      await serverFor(action.sessionId, action.sessionToken).unknown(action.runId, action.index).catch(() => undefined);
      handled.push(action.runId);
    }
    await store.remove(key);
  }
  for (const key of keys.filter((k) => k.startsWith(RUN_KEY_PREFIX))) {
    const run = await store.get<StoredRun>(key);
    if (run && !handled.includes(run.runId)) {
      await serverFor(run.sessionId, run.sessionToken).stop(run.runId, "EXECUTOR_LOST", { cause: "worker_restart" })
        .catch(() => undefined);
      handled.push(run.runId);
    }
    await store.remove(key);
  }
  return handled;
}
