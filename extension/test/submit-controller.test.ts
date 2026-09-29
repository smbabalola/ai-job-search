import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import type { ObservationV1 } from "../src/fill/observation-types";
import type { PhaseStore } from "../src/fill/run-controller";
import { observationFingerprint } from "../src/submit/fingerprint";
import {
  SUBMIT_KEY_PREFIX, SubmitController, recoverSubmitAfterRestart, type AuthorizationDirective, type SubmitPorts,
  type StoredSubmit,
} from "../src/submit/submit-controller";
import { SubmitServerError } from "../src/submit/server";
import { identifierRefs } from "./callgraph-helpers";

const vector = JSON.parse(readFileSync(resolve(__dirname, "../../tests/fixtures/submit/observation_fingerprint_vector.json"),
  "utf8")) as { observation: ObservationV1; observation_fingerprint: string };
const OBS = vector.observation;
const TOTAL = "sha256:" + "5".repeat(64);
const CONTROL = "sha256:" + "a".repeat(64);

function directive(): AuthorizationDirective {
  return {
    authorization_id: "hsa_1", grant_id: "gr_1", review_hash: "sha256:" + "r".repeat(64), expires_at: "x",
    certification_id: "greenhouse@2/submit@1",
    egress: [{ id: "E1_SUBMIT", methods: ["post"], types: ["main_frame"], regex: "^x$" }],
    confirmation_url: "https://jobs.example.test/acme/123/confirmation", e1_rule_id: 9201,
    expected: { canonical_url: OBS.context.canonical_url, observation_fingerprint: vector.observation_fingerprint,
                submit_control_fingerprint: CONTROL, ruleset_hash: TOTAL },
  };
}

type Signal = { success?: boolean; failure?: boolean; challenge?: boolean; rootPresent?: boolean };

interface World {
  calls: string[];
  ports: SubmitPorts;
  store: Map<string, unknown>;
  signals: Signal[];            // consumed one per poll (the last one repeats)
  observations: ObservationV1[]; // consumed one per observe (the last repeats)
  results: Record<string, unknown>[];
  preClick: () => Promise<{ attempt_id: string }>;
  dispatch: boolean;
  verifyTotal: boolean;
  verifyEgress: boolean;
  controlFound: boolean;
  clickResult: "CLICKED" | "SUBMIT_CONTROL_MISSING";
  contentChangedAt: number | null;  // poll index
  restored: boolean;
  detected: string[];
  siblings: boolean;
  matched: { available: boolean; ids: number[] };
  preClickBodies: Record<string, unknown>[];
  serverMatches: boolean;        // the server's revalidation of a CHALLENGE_CLEARED observation
  verifyEgressAfterClick: boolean;
}

function world(overrides: Partial<World> = {}): World {
  let now = 1_000;
  let polls = 0;
  const w: World = {
    calls: [], store: new Map(), signals: [{ success: true, rootPresent: false }], observations: [OBS], results: [],
    preClick: async () => ({ attempt_id: "att_1" }), dispatch: true, verifyTotal: true, verifyEgress: true,
    controlFound: true, clickResult: "CLICKED", contentChangedAt: null, restored: true, detected: [], siblings: true,
    matched: { available: true, ids: [9201] }, preClickBodies: [], serverMatches: true, verifyEgressAfterClick: true,
    ports: undefined as unknown as SubmitPorts, ...overrides,
  };
  const log = (c: string) => { w.calls.push(c); };
  const store: PhaseStore = {
    async get<T>(k: string) { return w.store.get(k) as T | undefined; },
    async set(k, v) { log(`store.set:${(v as StoredSubmit).phase}`); w.store.set(k, structuredClone(v)); },
    async remove(k) { log("store.remove"); w.store.delete(k); },
    async keys() { return [...w.store.keys()]; },
  };
  w.ports = {
    server: {
      async observation(_run, phase) {
        log(`observation:${phase}`);
        return phase === "CHALLENGE_CLEARED" ? { matches_review: w.serverMatches } : {};
      },
      async preClick(_run, body) { log("preClick"); w.preClickBodies.push(body as never); return w.preClick(); },
      async dispatch() { log("dispatch"); return { dispatched: w.dispatch }; },
      async event(_run, _att, event) { log(`event:${event}`); return {}; },
      async result(_run, _att, evidence) {
        log("result"); w.results.push(evidence);
        const e = evidence as Record<string, unknown>;
        const state = e.content_changed ? "SUBMISSION_AMBIGUOUS" : e.success_observed ? "CONFIRMED_SUCCESS"
          : e.failure_observed || e.click_performed === false ? "SUBMISSION_FAILED" : "SUBMISSION_AMBIGUOUS";
        return { state, reason: null };
      },
      async cancel() { log("cancel"); return {}; },
    },
    page: {
      async observe() { log("observe"); return w.observations.length > 1 ? w.observations.shift()! : w.observations[0]; },
      async detected() { return w.detected; },
      async watchContent() { log("watchContent"); },
      async contentChanged() { return w.contentChangedAt !== null && polls >= w.contentChangedAt; },
      async findSubmitControl() { return w.controlFound; },
      async clickSubmit() { log("clickSubmit"); return w.clickResult; },
      async signals() {
        polls += 1;
        const s = w.signals.length > 1 ? w.signals.shift()! : w.signals[0];
        return { success: false, failure: false, challenge: false, rootPresent: true, ...s };
      },
    },
    egress: {
      async verifyTotal() { log("verifyTotal"); return w.verifyTotal; },
      async install() { log("install"); return "sha256:egress"; },
      async verify() {
        log("verifyEgress");
        return w.calls.includes("clickSubmit") ? w.verifyEgressAfterClick : w.verifyEgress;
      },
      async restoreTotal() { log("restoreTotal"); return w.restored; },
      async matched() { log("matched"); return w.matched; },
    },
    browser: {
      async siblingsContained() { return w.siblings; },
      now: () => now,
      async sleep(ms) { now += ms; },
    },
    store,
    timing: { pollMs: 250, windowMs: 30_000, challengeMs: 300_000 },
  };
  return w;
}

const CTX = { tabId: 1, runId: "run_1", sessionId: "hs_1", sessionToken: "tok", executorInstanceId: "ex_1",
              browserSessionId: "b1" };

async function run(w: World) {
  const c = new SubmitController(w.ports, CTX);
  const view = await c.run(directive());
  return { c, view };
}

describe("SubmitController (6E-A spec §10)", () => {
  it("happy path: proofs, pre-click, durable dispatch, verified egress, one click, success, TOTAL restored", async () => {
    const w = world();
    const { view } = await run(w);
    expect(view.phase).toBe("SUBMITTED");
    expect(w.calls.filter((c) => !c.startsWith("event:") && c !== "observe")).toEqual([
      "watchContent", "verifyTotal", "preClick", "store.set:DISPATCHING", "dispatch", "store.set:DISPATCHED_ACK",
      "install", "verifyEgress", "store.set:CLICKED", "clickSubmit", "restoreTotal", "matched",
      "observation:POST_SUBMIT", "result", "store.remove"]);
    const verification = w.preClickBodies[0].verification as Record<string, unknown>;
    expect(verification).toEqual({ executor_instance_id: "ex_1", browser_session_id: "b1", execution_tab_id: 1,
      canonical_url: OBS.context.canonical_url, observation_fingerprint: await observationFingerprint(OBS),
      submit_control_fingerprint: CONTROL, ruleset_hash: TOTAL, challenge_visible: false });
    expect(w.results[0]).toMatchObject({ click_performed: true, egress_ever_installed: true,
      total_restored_verified: true, success_observed: true, content_changed: false, matched_rule_ids: [9201] });
    expect(w.calls).toContain("event:CLICK_PERFORMED");
    expect(w.calls).toContain("event:TOTAL_RESTORED");
  });

  it("J3 ordering: DISPATCHING is persisted before dispatch, and the egress is verified before the click", async () => {
    const w = world();
    await run(w);
    const at = (c: string) => w.calls.indexOf(c);
    expect(at("store.set:DISPATCHING")).toBeLessThan(at("dispatch"));
    expect(at("verifyEgress")).toBeLessThan(at("clickSubmit"));
    expect(at("store.set:CLICKED")).toBeLessThan(at("clickSubmit"));
    expect(w.calls.filter((c) => c === "clickSubmit")).toHaveLength(1);
  });

  it.each([
    ["a detection already recorded", { detected: ["POST_FILL_CHANGE_OBSERVED"] }, "DETECTION_OBSERVED"],
    ["TOTAL not verified", { verifyTotal: false }, "QUARANTINE_NOT_VERIFIED"],
    ["a sibling context open", { siblings: false }, "SIBLING_EMPLOYER_CONTEXT_OPEN"],
    ["the bound control missing", { controlFound: false }, "SUBMIT_CONTROL_MISSING"],
  ])("a local proof failure (%s) is sent as a refusal and nothing is dispatched", async (_, overrides, reason) => {
    const w = world({ ...overrides, preClick: async () => { throw new SubmitServerError(409, reason.toLowerCase()); } });
    const { view } = await run(w);
    expect((w.preClickBodies[0].verification as Record<string, unknown>).local_refusal).toBe(reason);
    expect(view.phase).toBe("NOT_SUBMITTED");
    expect(w.calls).not.toContain("dispatch");
    expect(w.calls).not.toContain("clickSubmit");
  });

  it("a visible challenge before the click is reported, and the refusal stops everything", async () => {
    const w = world({ signals: [{ challenge: true }],
                      preClick: async () => { throw new SubmitServerError(409, "challenge_before_submit"); } });
    const { view } = await run(w);
    expect((w.preClickBodies[0].verification as Record<string, unknown>).challenge_visible).toBe(true);
    expect(view).toMatchObject({ phase: "NOT_SUBMITTED", reason: "challenge_before_submit" });
    expect(w.calls).not.toContain("install");
  });

  it("a dispatch the server refuses never installs egress or clicks", async () => {
    const w = world({ dispatch: false });
    const { view } = await run(w);
    expect(view).toMatchObject({ phase: "NOT_SUBMITTED", reason: "DISPATCH_REFUSED" });
    expect(w.calls).not.toContain("install");
    expect(w.store.size).toBe(0);
  });

  it("an egress that does not verify restores TOTAL and reports no click", async () => {
    const w = world({ verifyEgress: false });
    await run(w);
    expect(w.calls).not.toContain("clickSubmit");
    expect(w.calls).toContain("event:EGRESS_VERIFY_FAILED");
    expect(w.results[0]).toMatchObject({ click_performed: false, egress_ever_installed: true,
      total_restored_verified: true, cause: "EGRESS_NOT_VERIFIED" });
  });

  it("a control missing at click time restores TOTAL and reports no click", async () => {
    const w = world({ clickResult: "SUBMIT_CONTROL_MISSING" });
    await run(w);
    expect(w.results[0]).toMatchObject({ click_performed: false, cause: "SUBMIT_CONTROL_MISSING" });
    expect(w.calls).toContain("event:SUBMIT_CONTROL_MISSING");
  });

  it("a certified failure signal is reported", async () => {
    const w = world({ signals: [{ failure: true }] });
    const { view } = await run(w);
    expect(w.results[0]).toMatchObject({ click_performed: true, failure_observed: true });
    expect(view.phase).toBe("NOT_SUBMITTED");
  });

  it("no signal within the 30 s window ends the watch and reports nothing observed (Review Focus 3)", async () => {
    const w = world({ signals: [{}] });
    const { view } = await run(w);
    expect(w.results[0]).toMatchObject({ click_performed: true, success_observed: false, failure_observed: false,
      cause: "NO_SIGNAL" });
    expect(view.phase).toBe("UNCLEAR");
    const polls = w.calls.filter((c) => c === "restoreTotal");
    expect(polls).toHaveLength(1);
  });

  it("a challenge after the click is handed to the human; an unchanged re-observation continues to success", async () => {
    const w = world({ signals: [{ challenge: true }, { challenge: true }, { challenge: false, rootPresent: true },
                                { success: true, rootPresent: false }], observations: [OBS] });
    const views: string[] = [];
    const c = new SubmitController(w.ports, CTX, (v) => views.push(v.phase));
    const view = await c.run(directive());
    expect(views).toContain("CHALLENGE");
    expect(w.calls).toContain("event:CHALLENGE_DETECTED");
    expect(w.calls).toContain("event:CHALLENGE_CLEARED");
    expect(w.calls).toContain("observation:CHALLENGE_CLEARED");
    expect(view.phase).toBe("SUBMITTED");
  });

  it("a changed page when the challenge clears restores TOTAL immediately and reports a content change", async () => {
    const changed = structuredClone(OBS);
    changed.elements[1].value_state = { state: "NONBLANK", current_value_hash: "sha256:" + "8".repeat(64) } as never;
    // [pre-click check, poll 1: challenge, poll 2: cleared (re-observe -> changed), poll 3: success]
    const w = world({ signals: [{}, { challenge: true }, { challenge: false, rootPresent: true }, { success: true }],
                      observations: [OBS, changed] });
    await run(w);
    expect(w.calls).toContain("event:CONTENT_CHANGED_DURING_ATTEMPT");
    expect(w.results[0]).toMatchObject({ content_changed: true, success_observed: false });
  });

  it("a person editing the form mid-attempt restores TOTAL immediately", async () => {
    const w = world({ signals: [{}], contentChangedAt: 2 });
    await run(w);
    const at = (c: string) => w.calls.indexOf(c);
    expect(at("event:CONTENT_CHANGED_DURING_ATTEMPT")).toBeGreaterThan(-1);
    expect(at("restoreTotal")).toBeGreaterThan(at("event:CONTENT_CHANGED_DURING_ATTEMPT"));
    expect(w.results[0]).toMatchObject({ content_changed: true });
  });

  it("the challenge window of 300 s ends the watch", async () => {
    const w = world({ signals: [{ challenge: true }] });
    await run(w);
    expect(w.results[0]).toMatchObject({ cause: "CHALLENGE_TIMEOUT" });
  });

  it("cancel before dispatch cancels on the server and never dispatches; after DISPATCHING it is refused", async () => {
    const w = world();
    let c!: SubmitController;
    w.preClick = async () => { expect(c.cancel()).toBe(true); return { attempt_id: "att_1" }; };
    c = new SubmitController(w.ports, CTX);
    const view = await c.run(directive());
    expect(view).toMatchObject({ phase: "NOT_SUBMITTED", reason: "CANCELLED" });
    expect(w.calls).toContain("cancel");
    expect(w.calls).not.toContain("dispatch");
    const late = world();
    const c2 = new SubmitController(late.ports, CTX);
    late.ports.server.dispatch = async () => { expect(c2.cancel()).toBe(false); return { dispatched: true }; };
    expect((await c2.run(directive())).phase).toBe("SUBMITTED");
  });

  it("the same grant is never run twice", async () => {
    const w = world();
    const c = new SubmitController(w.ports, CTX);
    await c.run(directive());
    await c.run(directive());
    expect(w.calls.filter((x) => x === "preClick")).toHaveLength(1);
  });

  it("a REVIEW request is answered once with the current observation", async () => {
    const w = world();
    const c = new SubmitController(w.ports, CTX);
    await c.observeForReview("req_1");
    await c.observeForReview("req_1");
    expect(w.calls.filter((x) => x === "observation:REVIEW")).toHaveLength(1);
  });
});

describe("restart recovery (6E-A J3)", () => {
  function stored(phase: StoredSubmit["phase"]): StoredSubmit {
    return { tabId: 1, runId: "run_1", sessionId: "hs_1", sessionToken: "tok", attemptId: "att_1", phase,
             dispatchedAt: 1_000, totalHash: TOTAL };
  }

  it.each([["CLICKED", "UNKNOWN", true], ["DISPATCHED_ACK", false, true], ["DISPATCHING", false, false]] as const)(
    "a persisted %s restores TOTAL and reports click_performed=%s without ever clicking", async (phase, click, egress) => {
      const w = world();
      w.store.set(SUBMIT_KEY_PREFIX + "1", stored(phase));
      await recoverSubmitAfterRestart(w.ports.store, () => w.ports.server, () => w.ports.egress);
      expect(w.calls).not.toContain("clickSubmit");
      expect(w.calls).toContain("restoreTotal");
      expect(w.calls).toContain("event:EXECUTOR_RESTARTED");
      expect(w.results[0]).toMatchObject({ click_performed: click, egress_ever_installed: egress,
        cause: "EXECUTOR_RESTARTED" });
      expect(w.store.size).toBe(0);
    });
});

describe("clickSubmit is invoked only by the SubmitController (6E-A E13)", () => {
  it("outside the page API, the page bundle and the background port forwarder, only submit-controller.ts calls it", () => {
    const callers = identifierRefs("clickSubmit").filter((r) => !r.startsWith("fill/page-api.ts")
      && !r.startsWith("fill/page-bundle.ts"));
    expect(new Set(callers.map((r) => r.split("#")[0]))).toEqual(
      new Set(["submit/submit-controller.ts", "background/submit-wiring.ts"]));
    // In the controller: the SubmitPagePort member declaration (no enclosing
    // function) and exactly one call, inside steps() after the proofs.
    const inController = callers.filter((r) => r.startsWith("submit/submit-controller.ts#"));
    const inFunctions = inController.filter((r) => !r.endsWith("#"));
    expect(inFunctions).toEqual(["submit/submit-controller.ts#steps"]);
  });
});

describe("revalidation after a challenge (6E-A condition: full fresh observation and revalidation)", () => {
  const cleared = [{}, { challenge: true }, { challenge: false, rootPresent: true }, { success: true }];

  it("the server's revalidation must agree before the attempt continues", async () => {
    const w = world({ signals: [...cleared], serverMatches: false });
    await run(w);
    expect(w.calls).toContain("event:CONTENT_CHANGED_DURING_ATTEMPT");
    expect(w.results[0]).toMatchObject({ content_changed: true, success_observed: false });
  });

  it("the SUBMIT egress is re-verified when the challenge clears; a changed ruleset stops the attempt", async () => {
    const w = world({ signals: [...cleared], verifyEgressAfterClick: false });
    await run(w);
    expect(w.calls.filter((c) => c === "verifyEgress")).toHaveLength(2);
    expect(w.calls).toContain("event:EGRESS_VERIFY_FAILED");
    expect(w.results[0]).toMatchObject({ success_observed: false, cause: "QUARANTINE_CHANGED" });
  });

  it("when all three agree the attempt continues to its signal", async () => {
    const w = world({ signals: [...cleared] });
    const { view } = await run(w);
    expect(view.phase).toBe("SUBMITTED");
    expect(w.calls.filter((c) => c === "verifyEgress")).toHaveLength(2);
  });
});

describe("a challenge that clears and succeeds in the same poll (browser finding)", () => {
  it("records CHALLENGE_CLEARED and re-verifies the egress before accepting the success", async () => {
    const w = world({ signals: [{}, { challenge: true }, { success: true, rootPresent: false }] });
    const { view } = await run(w);
    const at = (c: string) => w.calls.indexOf(c);
    expect(at("event:CHALLENGE_CLEARED")).toBeGreaterThan(-1);
    expect(at("event:CHALLENGE_CLEARED")).toBeLessThan(at("event:SIGNAL_OBSERVED"));
    expect(w.calls.filter((c) => c === "verifyEgress")).toHaveLength(2);
    expect(view.phase).toBe("SUBMITTED");
  });

  it("a changed egress at that moment still stops the attempt instead of accepting the success", async () => {
    const w = world({ signals: [{}, { challenge: true }, { success: true, rootPresent: false }],
                      verifyEgressAfterClick: false });
    await run(w);
    expect(w.results[0]).toMatchObject({ success_observed: false, cause: "QUARANTINE_CHANGED" });
  });
});

describe("an unexpected failure after the egress opened (J5: bounded egress)", () => {
  it("restores TOTAL and reports an unknown click instead of leaving the allow rules installed", async () => {
    const w = world();
    const setOrig = w.ports.store.set.bind(w.ports.store);
    w.ports.store.set = async (k, v) => {
      if ((v as StoredSubmit).phase === "CLICKED") throw new Error("storage quota");
      return setOrig(k, v);
    };
    const c = new SubmitController(w.ports, CTX);
    const view = await c.run(directive());
    expect(w.calls).not.toContain("clickSubmit");
    expect(w.calls).toContain("restoreTotal");
    expect(w.results[0]).toMatchObject({ click_performed: "UNKNOWN", cause: "EXTENSION_ERROR" });
    expect(view.phase).toBe("UNCLEAR");
  });

  it("an unexpected failure before any dispatch ends NOT_SUBMITTED without touching the egress", async () => {
    const w = world();
    w.ports.page.observe = async () => { throw new Error("frame gone"); };
    const view = await new SubmitController(w.ports, CTX).run(directive());
    expect(view).toMatchObject({ phase: "NOT_SUBMITTED", reason: "EXTENSION_ERROR" });
    expect(w.calls).not.toContain("dispatch");
    expect(w.calls).not.toContain("install");
  });
});

describe("the form vanishing during the clearance re-observation (browser finding, 1 in 4 runs)", () => {
  // [pre-click check, poll 1: challenge, poll 2: cleared with the form still seen, poll 3: success]
  const race = [{}, { challenge: true }, { challenge: false, rootPresent: true }, { success: true, rootPresent: false }];

  it("a re-observation that finds no application root means the form is gone, not changed", async () => {
    const gone = structuredClone(OBS);
    gone.context.application_root_found = false;
    gone.elements = [];
    const w = world({ signals: [...race], observations: [OBS, gone] });
    const { view } = await run(w);
    expect(w.calls).not.toContain("event:CONTENT_CHANGED_DURING_ATTEMPT");
    expect(view.phase).toBe("SUBMITTED");
  });

  it("a re-observation that fails because the page is mid-swap is treated the same way", async () => {
    const w = world({ signals: [...race] });
    let n = 0;
    const observe = w.ports.page.observe;
    w.ports.page.observe = async () => { n += 1; if (n === 2) throw new Error("frame replaced"); return observe(); };
    const { view } = await run(w);
    expect(w.calls).not.toContain("event:CONTENT_CHANGED_DURING_ATTEMPT");
    expect(view.phase).toBe("SUBMITTED");
  });

  it("a PRESENT form that differs is still a content change", async () => {
    const changed = structuredClone(OBS);
    changed.elements[1].value_state = { state: "NONBLANK", current_value_hash: "sha256:" + "8".repeat(64) } as never;
    const w = world({ signals: [...race], observations: [OBS, changed] });
    await run(w);
    expect(w.calls).toContain("event:CONTENT_CHANGED_DURING_ATTEMPT");
  });
});
