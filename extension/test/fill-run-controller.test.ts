import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";
import { greenhouseFill } from "../src/fill/certified-adapters";
import { installDetections, type DetectionHandle } from "../src/fill/detections";
import { executeAction, type Envelope, type PlanAction } from "../src/fill/executor";
import { fillValueHash } from "../src/fill/hash";
import type { ObservationV1 } from "../src/fill/observation-types";
import { observe } from "../src/fill/observer";
import {
  ACTION_KEY_PREFIX, FillRunController, RUN_KEY_PREFIX, recoverAfterRestart, type ControllerView, type FillPorts,
  type PhaseStore,
} from "../src/fill/run-controller";
import type { FillServer, LocalDocument, PlanStatusAction } from "../src/fill/server";
import { sha256HexSync } from "../src/fill/sha256";
import { FILLED_TEXT, renderFillView } from "../src/popup/fill-view";

const here = path.dirname(fileURLToPath(import.meta.url));
const URL_ = "https://boards.greenhouse.io/acme/jobs/123";
const VALUES: Record<string, string> = { "gh:email": "ada@example.com", "gh:notice": "1 month" };
const CV = new TextEncoder().encode("%PDF-1.4 approved cv");
const CV_DOC = { document_version_id: "dv_cv", sha256: sha256HexSync(CV), byte_length: CV.length, filename: "cv.pdf",
  media_type: "application/pdf" };

type Call = [string, ...unknown[]];

function memoryStore(): PhaseStore & { data: Map<string, unknown> } {
  const data = new Map<string, unknown>();
  return {
    data,
    get: async <T>(key: string) => data.get(key) as T | undefined,
    set: async (key, value) => { data.set(key, structuredClone(value)); },
    remove: async (key) => { data.delete(key); },
    keys: async () => [...data.keys()],
  };
}

class World {
  dom: JSDOM;
  calls: Call[] = [];
  views: ControllerView[] = [];
  store = memoryStore();
  actions: PlanStatusAction[] = [];
  sleeps = 0;
  executes = 0;
  detections: DetectionHandle | null = null;
  controller!: FillRunController;
  // Scenario knobs
  permissions = true;
  siblings = true;
  otherTab: number | null = null;
  initial: Record<string, unknown> = { state: "REVALIDATING" };
  grantOk = true;
  refuseIntentAt: number | null = null;
  mountAfterSleeps = 0;
  afterOutcome: (index: number) => void = () => {};
  duringIntent: (index: number) => Promise<void> = async () => {};
  executeThrowsAt: number | null = null;
  filled = false;
  deliverMessages = true;  // false: the page's detection message is lost / arrives late

  constructor() {
    this.dom = new JSDOM(readFileSync(path.join(here, "fixtures", "fill-greenhouse.html"), "utf-8"), { url: URL_ });
    const proto = this.dom.window.HTMLInputElement.prototype;
    const files = new WeakMap<object, unknown>();
    Object.defineProperty(proto, "files", { configurable: true, get() { return files.get(this) ?? []; },
      set(value) { files.set(this, value); } });
  }

  get document(): Document {
    return this.dom.window.document;
  }

  async plan(): Promise<void> {
    const obs = await observe(this.document, greenhouseFill, { canonicalUrl: URL_, origin: new URL(URL_).origin });
    const fp = (key: string) => obs.elements.find((e) => e.page_field_key === key)!.field_fingerprint;
    this.actions = [
      { page_field_key: "gh:email", field_fingerprint: fp("gh:email"), action_kind: "WRITE",
        rendered_value_hash: await fillValueHash(VALUES["gh:email"]), document: null, document_kind: null },
      { page_field_key: "gh:notice", field_fingerprint: fp("gh:notice"), action_kind: "WRITE",
        rendered_value_hash: await fillValueHash(VALUES["gh:notice"]), document: null, document_kind: null },
      { page_field_key: "gh:resume", field_fingerprint: fp("gh:resume"), action_kind: "ATTACH_LOCAL",
        rendered_value_hash: await fillValueHash(CV_DOC.sha256), document: CV_DOC, document_kind: "cv" },
      { page_field_key: "gh:cover_letter", field_fingerprint: fp("gh:cover_letter"), action_kind: "OMIT",
        rendered_value_hash: null, document: null, document_kind: null },
      { page_field_key: "gh:name:authenticity_token", field_fingerprint: fp("gh:name:authenticity_token"),
        action_kind: "IGNORE_NON_APPLICATION", rendered_value_hash: null, document: null, document_kind: null },
    ];
  }

  server(): FillServer {
    const log = (...call: Call) => { this.calls.push(call); };
    const world = this;
    return {
      async startRun(identity) { log("startRun", identity); return { id: "frun_1" }; },
      async observation(_run, phase, obs, index) {
        log("observation", phase, index ?? null, obs.context.application_root_found);
        if (phase === "INITIAL") return world.initial as { state: string };
        if (phase === "REVALIDATION") {
          return obs.context.application_root_found ? { state: "REVALIDATING", matched: true }
            : { state: "UNSUPPORTED_FORM", causes: ["RESET_SURFACE_MISMATCH"] };
        }
        return { state: "FILLING" };
      },
      async planStatus() {
        log("planStatus");
        return { run_id: "frun_1", state: world.filled ? "FILLED_AWAITING_SUBMISSION" : "FILLING", reason: null,
          grant_id: "grant_1", ruleset_hash: "sha256:total", plan_hash: "sha256:plan", structure_fingerprint: null,
          actions: world.actions };
      },
      async quarantine(_run, phase, hash) {
        log("quarantine", phase, hash);
        if (phase === "LOST") return { state: "FILL_STOPPED", reason: "QUARANTINE_LOST" };
        return { state: phase === "TOTAL_VERIFIED" ? "QUARANTINE_ACTIVE" : "REVALIDATING" };
      },
      async grant() {
        log("grant");
        return world.grantOk ? { granted: true, grant_id: "grant_1", stop_reason: null }
          : { granted: false, grant_id: null, stop_reason: "GRANT_REFUSED" };
      },
      async intent(_run, index, precheck) {
        log("intent", index, precheck);
        await world.duringIntent(index);
        if (index === world.refuseIntentAt) return { envelope: null, stop_reason: "AUTHORITY_REDUCED",
          detail: { reduction: "KILL_SWITCH" } };
        const action = world.actions[index];
        const writes = action.action_kind === "WRITE" || action.action_kind === "ATTACH_LOCAL";
        const envelope: Envelope = { envelope_id: writes ? `fenv_${index}` : null, action_kind: action.action_kind,
          rendered_value: VALUES[action.page_field_key] ?? null, rendered_value_hash: action.rendered_value_hash,
          document: action.document };
        return { envelope, stop_reason: null, detail: {} };
      },
      async outcome(_run, index, body) {
        log("outcome", index, body.outcome, body.envelope_id);
        world.afterOutcome(index);
        if (!body.outcome.endsWith("VERIFIED") && body.outcome !== "IGNORE_RECORDED"
            && body.outcome !== "NOOP_ALREADY_EQUAL") {
          return { state: "FILL_STOPPED", reason: body.outcome };
        }
        return { state: index === world.actions.length - 1 ? "FINAL_VALIDATING" : "FILLING" };
      },
      async unknown(_run, index) { log("unknown", index); return { state: "FILL_STOPPED", reason: "WRITE_OUTCOME_UNKNOWN" }; },
      async heartbeat() { log("heartbeat"); return { state: "FILLING", lease_expired: false }; },
      async detection(_run, kind) {
        log("detection", kind);
        return world.filled ? { state: "FILLED_AWAITING_SUBMISSION" } : { state: "FILL_STOPPED", reason: kind };
      },
      async final() { log("final"); world.filled = true; return { state: "FILLED_AWAITING_SUBMISSION" }; },
      async stop(_run, reason) { log("stop", reason); return { state: "FILL_STOPPED", reason }; },
      async document(kind): Promise<LocalDocument> {
        log("document", kind);
        return { bytes: CV, filename: "cv.pdf", mediaType: "application/pdf", sha256: CV_DOC.sha256 };
      },
    };
  }

  ports(): FillPorts {
    const world = this;
    let mounted = this.mountAfterSleeps === 0;
    return {
      server: this.server(),
      store: this.store,
      timers: { setInterval: () => "hb", clearInterval: () => {} },
      timing: { pollMs: 1000, mountTimeoutMs: 10_000 },
      page: {
        async rootPresent() { return mounted; },
        async observe(): Promise<ObservationV1> {
          const obs = await observe(world.document, greenhouseFill, { canonicalUrl: URL_, origin: new URL(URL_).origin });
          if (!mounted) obs.context.application_root_found = false;
          return obs;
        },
        async execute(action: PlanAction, envelope: Envelope | null, attachment: LocalDocument | null) {
          world.executes += 1;
          world.calls.push(["execute", world.actions.indexOf(action as unknown as PlanStatusAction),
            [...world.store.data.keys()].some((k) => k.startsWith(ACTION_KEY_PREFIX))]);
          if (world.executeThrowsAt === world.actions.indexOf(action as unknown as PlanStatusAction)) {
            throw new Error("the page went away");
          }
          const file = attachment ? new world.dom.window.File([attachment.bytes], attachment.filename,
            { type: attachment.mediaType }) : null;
          return executeAction(world.document, greenhouseFill, action, envelope, file,
            { makeFileList: (f) => [f] as unknown as FileList, settleTiming: { quietMs: 2, capMs: 200 } });
        },
        async installDetections(runId: string) {
          world.detections = installDetections(world.dom.window as unknown as Window, (kind, detail) => {
            world.calls.push(["page-detected", kind, runId]);
            if (world.deliverMessages) void world.controller.onDetection(kind, detail);
          });
        },
        async detected() { return world.detections?.reported() ?? []; },
        async enablePostFill() { world.calls.push(["enablePostFill"]); },
      },
      quarantine: {
        async otherQuarantinedTab() { return world.otherTab; },
        async install(kind) { world.calls.push(["install", kind]); return `sha256:${kind.toLowerCase()}`; },
        async verify() { return true; },
        async remove() { world.calls.push(["removeRuleset"]); },
      },
      browser: {
        async permissionsGranted() { return world.permissions; },
        async siblingsContained() { return world.siblings; },
        async reloadAndWait() { world.calls.push(["reload"]); },
        async sleep() {
          world.sleeps += 1;
          if (world.sleeps >= world.mountAfterSleeps) mounted = true;
        },
        async setBadge(text) { world.calls.push(["badge", text]); },
      },
    };
  }

  async run(): Promise<ControllerView> {
    await this.plan();
    this.controller = new FillRunController(this.ports(), { tabId: 7, sessionId: "hs_1", sessionToken: "tok",
      executorInstanceId: "exec_1", browserSessionId: "bs_1" }, (view) => this.views.push(view));
    const view = await this.controller.run();
    this.detections?.dispose();
    return view;
  }

  names(): string[] {
    return this.calls.map((c) => (c[0] === "observation" || c[0] === "quarantine" || c[0] === "install")
      ? `${c[0]}:${c[1]}` : c[0]);
  }

  value(id: string): string {
    return (this.document.getElementById(id) as HTMLInputElement).value;
  }
}

describe("the run controller (spec §11.1)", () => {
  it("walks every transition to FILLED", async () => {
    const w = new World();
    const view = await w.run();
    expect(view).toMatchObject({ phase: "FILLED", runId: "frun_1", progress: { done: 5, total: 5 } });
    expect(w.views.map((v) => v.phase).filter((p, i, a) => p !== a[i - 1]))
      .toEqual(["OBSERVING", "PREPARING", "FILLING", "FILLED"]);
    const names = w.names();
    expect(names.slice(0, 11)).toEqual(["startRun", "observation:INITIAL", "install:PRELOAD",
      "quarantine:PRELOAD_INSTALLED", "reload", "badge", "quarantine:RELOADED", "observation:REVALIDATION",
      "install:TOTAL", "quarantine:TOTAL_VERIFIED", "grant"]);
    expect(names.filter((n) => n === "intent")).toHaveLength(5);
    expect(names.slice(-3)).toEqual(["outcome", "final", "enablePostFill"]);
    expect([w.value("email"), w.value("notice"), w.value("cover_letter")]).toEqual(["ada@example.com", "1 month", ""]);
    const outcomes = w.calls.filter((c) => c[0] === "outcome").map((c) => c[2]);
    expect(outcomes).toEqual(["WRITTEN_VERIFIED", "WRITTEN_VERIFIED", "ATTACH_LOCAL_VERIFIED", "OMIT_VERIFIED",
      "IGNORE_RECORDED"]);
    expect(w.store.data.has(RUN_KEY_PREFIX + 7)).toBe(true);  // FILLED keeps the run (lease continuity)
    expect([...w.store.data.keys()].some((k) => k.startsWith(ACTION_KEY_PREFIX))).toBe(false);
  });

  it("persists MAY_HAVE_WRITTEN before every write, never before OMIT/IGNORE", async () => {
    const w = new World();
    await w.run();
    const flags = w.calls.filter((c) => c[0] === "execute").map((c) => c[2]);
    expect(flags).toEqual([true, true, true, false, false]);
  });

  it.each([
    ["the plan needs review", (w: World) => { w.initial = { state: "PLAN_NEEDS_REVIEW" }; }, "NEEDS_REVIEW"],
    ["the form is unsupported", (w: World) => { w.initial = { state: "UNSUPPORTED_FORM" }; }, "UNSUPPORTED"],
  ])("ends before any quarantine when %s", async (_, setup, phase) => {
    const w = new World();
    setup(w);
    expect((await w.run()).phase).toBe(phase);
    expect(w.names()).not.toContain("install:PRELOAD");
  });

  it.each([
    ["permissions are missing", (w: World) => { w.permissions = false; }, "PERMISSIONS_MISSING"],
    ["a sibling employer context is open", (w: World) => { w.siblings = false; }, "SIBLING_EMPLOYER_CONTEXT_OPEN"],
    ["another tab is quarantined", (w: World) => { w.otherTab = 3; }, "QUARANTINE_RULESET_CHANGED"],
  ])("stops before observing when %s", async (_, setup, reason) => {
    const w = new World();
    setup(w);
    const view = await w.run();
    expect(view).toMatchObject({ phase: "STOPPED", reason });
    expect(w.calls.find((c) => c[0] === "stop")).toEqual(["stop", reason]);
    expect(w.names()).not.toContain("observation:INITIAL");
  });

  it("a refused grant stops the run", async () => {
    const w = new World();
    w.grantOk = false;
    expect(await w.run()).toMatchObject({ phase: "STOPPED", reason: "GRANT_REFUSED" });
    expect(w.executes).toBe(0);
  });

  it("a refused intent stops with no DOM call", async () => {
    const w = new World();
    w.refuseIntentAt = 1;
    expect(await w.run()).toMatchObject({ phase: "STOPPED", reason: "AUTHORITY_REDUCED" });
    expect(w.executes).toBe(1);  // only action 0 ran
    expect(w.value("notice")).toBe("");
  });

  it("a lost execution after MAY_HAVE_WRITTEN is reported unknown and never retried", async () => {
    const w = new World();
    w.executeThrowsAt = 1;
    expect(await w.run()).toMatchObject({ phase: "STOPPED", reason: "WRITE_OUTCOME_UNKNOWN" });
    expect(w.calls.filter((c) => c[0] === "unknown")).toEqual([["unknown", 1]]);
    expect(w.calls.filter((c) => c[0] === "intent")).toHaveLength(2);
  });
});

describe("worker restart (spec §11.3)", () => {
  it("turns a stored MAY_HAVE_WRITTEN into unknown and stops other runs; nothing resumes", async () => {
    const store = memoryStore();
    await store.set(ACTION_KEY_PREFIX + "frun_1", { runId: "frun_1", tabId: 7, sessionId: "hs_1", sessionToken: "t",
      index: 2, envelopeId: "fenv_2", phase: "MAY_HAVE_WRITTEN" });
    await store.set(RUN_KEY_PREFIX + 7, { runId: "frun_1", tabId: 7, sessionId: "hs_1", sessionToken: "t" });
    await store.set(RUN_KEY_PREFIX + 9, { runId: "frun_2", tabId: 9, sessionId: "hs_2", sessionToken: "u" });
    const calls: Call[] = [];
    const server = (sessionId: string) => ({
      unknown: async (run: string, index: number) => { calls.push(["unknown", sessionId, run, index]); return { state: "x" }; },
      stop: async (run: string, reason: string) => { calls.push(["stop", sessionId, run, reason]); return { state: "x" }; },
    }) as unknown as FillServer;
    expect(await recoverAfterRestart(store, server)).toEqual(["frun_1", "frun_2"]);
    expect(calls).toEqual([["unknown", "hs_1", "frun_1", 2], ["stop", "hs_2", "frun_2", "EXECUTOR_LOST"]]);
    expect(store.data.size).toBe(0);
  });
});

describe("the reset waits for a late form (Review Focus 4)", () => {
  it("compares only after the root mounts at 3 s", async () => {
    const w = new World();
    w.mountAfterSleeps = 3;
    expect((await w.run()).phase).toBe("FILLED");
    expect(w.sleeps).toBe(3);
    const revalidation = w.calls.find((c) => c[0] === "observation" && c[1] === "REVALIDATION")!;
    expect(revalidation[3]).toBe(true);  // application_root_found at comparison time
  });

  it("a root that never mounts is UNSUPPORTED_FORM(RESET_SURFACE_MISMATCH)", async () => {
    const w = new World();
    w.mountAfterSleeps = 99;
    const view = await w.run();
    expect(view).toMatchObject({ phase: "UNSUPPORTED" });
    expect(view.detail.causes).toEqual(["RESET_SURFACE_MISMATCH"]);
    expect(w.sleeps).toBe(10);  // REVALIDATION_MOUNT_TIMEOUT / SETTLE_CAP polling, then no longer
    expect(w.executes).toBe(0);
  });
});

describe("the user touching the page mid-fill (Review Focus 5)", () => {
  it("typing into a pending target is PREFILLED_VALUE_CONFLICT, never an overwrite", async () => {
    const w = new World();
    w.afterOutcome = (index) => {
      if (index === 0) (w.document.getElementById("notice") as HTMLInputElement).value = "3 months";
    };
    expect(await w.run()).toMatchObject({ phase: "STOPPED", reason: "PREFILLED_VALUE_CONFLICT" });
    expect(w.value("notice")).toBe("3 months");
    expect(w.calls.filter((c) => c[0] === "intent")).toHaveLength(1);
  });

  it("editing a completed field is FIELD_VALUE_REVERTED", async () => {
    const w = new World();
    w.afterOutcome = (index) => {
      if (index === 1) (w.document.getElementById("email") as HTMLInputElement).value = "other@example.com";
    };
    expect(await w.run()).toMatchObject({ phase: "STOPPED", reason: "FIELD_VALUE_REVERTED" });
    expect(w.calls.filter((c) => c[0] === "intent")).toHaveLength(2);
  });
});

describe("detections", () => {
  it("a submit event during FILLING is cancelled and stops the run", async () => {
    const w = new World();
    let prevented: boolean | null = null;
    w.afterOutcome = (index) => {
      if (index !== 0) return;
      const form = w.document.getElementById("application_form") as HTMLFormElement;
      const event = new w.dom.window.Event("submit", { bubbles: true, cancelable: true });
      form.dispatchEvent(event);
      prevented = event.defaultPrevented;
    };
    await w.run();
    expect(prevented).toBe(true);
    expect(w.calls.filter((c) => c[0] === "detection")).toEqual([["detection", "SUBMIT_ATTEMPT_OBSERVED"]]);
    expect(w.controller.view).toMatchObject({ phase: "STOPPED", reason: "SUBMIT_ATTEMPT_OBSERVED" });
    expect(w.executes).toBe(1);  // no DOM call after the stop
    expect(w.value("notice")).toBe("");
  });

  it("a detection whose message never arrives still stops the run at the next step", async () => {
    const w = new World();
    w.deliverMessages = false;
    w.afterOutcome = (index) => {
      if (index !== 0) return;
      const form = w.document.getElementById("application_form") as HTMLFormElement;
      form.dispatchEvent(new w.dom.window.Event("submit", { bubbles: true, cancelable: true }));
    };
    expect(await w.run()).toMatchObject({ phase: "STOPPED", reason: "SUBMIT_ATTEMPT_OBSERVED" });
    expect(w.executes).toBe(1);
    expect(w.calls.filter((c) => c[0] === "detection")).toEqual([["detection", "SUBMIT_ATTEMPT_OBSERVED"]]);
  });

  it("a late detection never overwrites how a finished run ended", async () => {
    const w = new World();
    w.refuseIntentAt = 1;  // the run ends STOPPED(AUTHORITY_REDUCED) ...
    await w.run();
    const server = w.ports().server;
    // ... then the page's detection message arrives; for an ended run the
    // server answers with its state only (no reason).
    server.detection = async () => ({ state: "FILL_STOPPED" });
    (w.controller as unknown as { ports: { server: FillServer } }).ports.server = server;
    await w.controller.onDetection("NAVIGATION_ATTEMPT_OBSERVED", {});
    expect(w.controller.view).toMatchObject({ phase: "STOPPED", reason: "AUTHORITY_REDUCED" });
  });

  it("heartbeats on the pinned interval while the run is live", async () => {
    const w = new World();
    const ticks: { fn: () => void; ms: number }[] = [];
    const ports = w.ports();
    ports.timers = { setInterval: (fn, ms) => { ticks.push({ fn, ms }); return ticks.length; }, clearInterval: () => {} };
    delete ports.timing;
    ports.browser.sleep = async () => {};
    await w.plan();
    w.controller = new FillRunController(ports, { tabId: 7, sessionId: "hs_1", sessionToken: "tok",
      executorInstanceId: "exec_1", browserSessionId: "bs_1" });
    await w.controller.run();
    expect(ticks.map((t) => t.ms)).toEqual([10_000]);
    ticks[0].fn();
    await Promise.resolve();
    expect(w.calls.some((c) => c[0] === "heartbeat")).toBe(true);
  });

  it("a detection that lands while an intent is in flight still prevents the write", async () => {
    const w = new World();
    w.duringIntent = async (index) => {
      if (index === 1) await w.controller.onDetection("NAVIGATION_ATTEMPT_OBSERVED", { cause: "pagehide" });
    };
    expect(await w.run()).toMatchObject({ phase: "STOPPED", reason: "NAVIGATION_ATTEMPT_OBSERVED" });
    expect(w.executes).toBe(1);
    expect(w.value("notice")).toBe("");
  });

  it("after FILLED a detection is an event only", async () => {
    const w = new World();
    await w.run();
    await w.controller.onDetection("SUBMIT_ATTEMPT_OBSERVED", {});
    expect(w.controller.view.phase).toBe("FILLED");
  });

  it("closing the tab records EXECUTION_CONTEXT_CLOSED and removes the rules", async () => {
    const w = new World();
    await w.run();
    await w.controller.onTabClosed();
    expect(w.names().slice(-2)).toEqual(["detection", "removeRuleset"]);
    expect(w.store.data.has(RUN_KEY_PREFIX + 7)).toBe(false);
  });
});

describe("the popup (spec §16.3)", () => {
  const phases: ControllerView["phase"][] = ["OBSERVING", "NEEDS_REVIEW", "UNSUPPORTED", "PREPARING", "FILLING",
    "FILLED", "STOPPED"];

  it.each(phases)("never renders a release control (%s)", (phase) => {
    const dom = new JSDOM(`<div id="app">${renderFillView({ phase, runId: "r", reason: "X", detail: {},
      progress: { done: 1, total: 5 } }, true)}</div>`);
    const text = dom.window.document.body.textContent!.toLowerCase();
    const controls = [...dom.window.document.querySelectorAll("button, a")].map((b) => b.textContent!.toLowerCase());
    expect(controls.some((c) => /release|lift|unquarantine|resume|submit/.test(c))).toBe(false);
    expect(text).not.toMatch(/release|unquarantine/);
    expect(dom.window.document.getElementById("abort-fill")?.textContent).toBe("Abort: close this tab");
  });

  it("shows the exact FILLED statement and escapes reasons", () => {
    expect(renderFillView({ phase: "FILLED", runId: "r", reason: null, detail: {}, progress: null }, true))
      .toContain(FILLED_TEXT);
    expect(renderFillView({ phase: "STOPPED", runId: "r", reason: "<img src=x>", detail: {}, progress: null }, true))
      .not.toContain("<img");
    expect(renderFillView(null, false)).toContain("Enable safe FILL");
  });
});

describe("a new run on the same tab (spec §10.7, §16.4)", () => {
  it("is refused while the tab still holds a quarantine: the only exit is closing it", async () => {
    const { mayStartOnTab } = await import("../src/fill/run-controller");
    expect(mayStartOnTab(null, true)).toBe(false);          // fill rules still installed for this tab
    expect(mayStartOnTab("FILLED", false)).toBe(false);     // a filled page is never re-run in place
    expect(mayStartOnTab("FILLING", false)).toBe(false);    // a live run
    expect(mayStartOnTab(null, false)).toBe(true);
    expect(mayStartOnTab("NEEDS_REVIEW", false)).toBe(true); // ended before any quarantine
    expect(mayStartOnTab("UNSUPPORTED", false)).toBe(true);
    expect(mayStartOnTab("STOPPED", false)).toBe(true);
  });
});
