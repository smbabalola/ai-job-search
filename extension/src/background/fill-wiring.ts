// Bundle 6D-B background wiring: binds the run controller's ports to chrome.*
// (scripting in the ISOLATED world on the user-activated tab, session DNR
// rules, tabs/webNavigation for sibling containment, storage.session for the
// persisted action phase) and to the typed fill server client.

import { installRuleset, removeRuleset, verifyRuleset } from "../fill/quarantine";
import { checkSiblingContainment, SAFE_FILL_OPTIONAL_PERMISSIONS } from "../fill/siblings";
import { FILL_PAGE_KEY, type FillPageApi } from "../fill/page-api";
import {
  FillRunController, mayStartOnTab, recoverAfterRestart, type ControllerView, type FillPorts, type PhaseStore,
} from "../fill/run-controller";
import { HttpFillServer, type LocalDocument } from "../fill/server";
import { isOpenerCreated } from "../fill/detections";
import type { Envelope, PlanAction } from "../fill/executor";
import { forgetSubmitTab, recoverSubmits, routeSubmitBeat } from "./submit-wiring";

const PAGE_BUNDLE = "fill-page/index.js";
const FILL_RULE_ID_SET = new Set([9101, 9102, 9111, 9121]);
const EXECUTOR_ID_KEY = "fill_executor_instance_id";
const BROWSER_SESSION_KEY = "fill_browser_session_id";

export interface FillSession { sessionId: string; sessionToken: string }

const controllers = new Map<number, FillRunController>();
const views = new Map<number, ControllerView>();

function randomId(prefix: string): string {
  return `${prefix}_${[...crypto.getRandomValues(new Uint8Array(10))].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

// executor_instance_id: one per installed, paired extension (local storage);
// browser_session_id: one per browser start (session storage, spec §11.4).
async function identity(): Promise<{ executorInstanceId: string; browserSessionId: string }> {
  const local = await chrome.storage.local.get(EXECUTOR_ID_KEY);
  let executorInstanceId = local[EXECUTOR_ID_KEY] as string | undefined;
  if (!executorInstanceId) {
    executorInstanceId = randomId("exec");
    await chrome.storage.local.set({ [EXECUTOR_ID_KEY]: executorInstanceId });
  }
  const session = await chrome.storage.session.get(BROWSER_SESSION_KEY);
  let browserSessionId = session[BROWSER_SESSION_KEY] as string | undefined;
  if (!browserSessionId) {
    browserSessionId = randomId("bs");
    await chrome.storage.session.set({ [BROWSER_SESSION_KEY]: browserSessionId });
  }
  return { executorInstanceId, browserSessionId };
}

export const sessionStore: PhaseStore = {
  async get<T>(key: string) {
    return (await chrome.storage.session.get(key))[key] as T | undefined;
  },
  async set(key, value) {
    await chrome.storage.session.set({ [key]: value });
  },
  async remove(key) {
    await chrome.storage.session.remove(key);
  },
  async keys() {
    return Object.keys(await chrome.storage.session.get(null));
  },
};

async function inject(tabId: number): Promise<void> {
  await chrome.scripting.executeScript({ target: { tabId }, files: [PAGE_BUNDLE] });
}

export async function detectCertifiedAdapter(tabId: number)
  : Promise<{ adapterId: string; adapterVersion: string } | null> {
  await inject(tabId);
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (key: string) => ((globalThis as unknown as Record<string, FillPageApi>)[key]).detect(),
    args: [FILL_PAGE_KEY],
  });
  return (result as { adapterId: string; adapterVersion: string } | null) ?? null;
}

function pagePort(tabId: number, adapterId: string, canonicalUrl: string, origin: string): FillPorts["page"] {
  return {
    async rootPresent() {
      await inject(tabId);
      const [{ result }] = await chrome.scripting.executeScript({ target: { tabId },
        func: (key: string, id: string) => ((globalThis as unknown as Record<string, FillPageApi>)[key]).rootPresent(id),
        args: [FILL_PAGE_KEY, adapterId] });
      return result === true;
    },
    async observe() {
      await inject(tabId);
      const [{ result }] = await chrome.scripting.executeScript({ target: { tabId },
        func: (key: string, id: string, ctx: { canonicalUrl: string; origin: string }) =>
          ((globalThis as unknown as Record<string, FillPageApi>)[key]).observe(id, ctx),
        args: [FILL_PAGE_KEY, adapterId, { canonicalUrl, origin }] });
      return result as Awaited<ReturnType<FillPageApi["observe"]>>;
    },
    async execute(action: PlanAction, envelope: Envelope | null, attachment: LocalDocument | null) {
      await inject(tabId);
      const payload = attachment
        ? { bytes: Array.from(attachment.bytes), filename: attachment.filename, mediaType: attachment.mediaType }
        : null;
      const [{ result }] = await chrome.scripting.executeScript({ target: { tabId },
        func: (key: string, id: string, a: PlanAction, e: Envelope | null, f: unknown) =>
          ((globalThis as unknown as Record<string, FillPageApi>)[key]).execute(id, a, e,
            f as Parameters<FillPageApi["execute"]>[3]),
        args: [FILL_PAGE_KEY, adapterId, action, envelope, payload] });
      return result as Awaited<ReturnType<FillPageApi["execute"]>>;
    },
    async installDetections(runId: string) {
      await inject(tabId);
      await chrome.scripting.executeScript({ target: { tabId },
        func: (key: string, id: string) => ((globalThis as unknown as Record<string, FillPageApi>)[key]).installDetections(id),
        args: [FILL_PAGE_KEY, runId] });
    },
    async detected() {
      const [{ result }] = await chrome.scripting.executeScript({ target: { tabId },
        func: (key: string) => ((globalThis as unknown as Record<string, FillPageApi>)[key]).detected(),
        args: [FILL_PAGE_KEY] });
      return (result as string[] | undefined) ?? [];
    },
    async enablePostFill() {
      await chrome.scripting.executeScript({ target: { tabId },
        func: (key: string, id: string) => ((globalThis as unknown as Record<string, FillPageApi>)[key]).enablePostFill(id),
        args: [FILL_PAGE_KEY, adapterId] });
    },
  };
}

function quarantinePort(tabId: number, employerHost: string): FillPorts["quarantine"] {
  return {
    async otherQuarantinedTab(own: number) {
      const rules = await chrome.declarativeNetRequest.getSessionRules();
      for (const rule of rules) {
        if (!FILL_RULE_ID_SET.has(rule.id)) continue;
        const tabs = rule.condition.tabIds ?? [];
        const other = tabs.find((id) => id !== own && id !== -1);
        if (other !== undefined) return other;
      }
      return null;
    },
    install: async (kind) => (await installRuleset(kind, tabId, employerHost)).rulesetHash,
    verify: (hash) => verifyRuleset(hash),
    remove: () => removeRuleset(),
  };
}

function reloadAndWait(tabId: number, timeoutMs = 30_000): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("reload timed out"));
    }, timeoutMs);
    const listener = (id: number, info: { status?: string }) => {
      if (id === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    void chrome.tabs.reload(tabId);
  });
}

function browserPort(tabId: number, origin: string): FillPorts["browser"] {
  return {
    permissionsGranted: () => chrome.permissions.contains({ permissions: [...SAFE_FILL_OPTIONAL_PERMISSIONS] }),
    siblingsContained: async () => (await checkSiblingContainment(tabId, origin)).ok,
    reloadAndWait: () => reloadAndWait(tabId),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    setBadge: async (text) => {
      await chrome.action.setBadgeBackgroundColor({ tabId, color: "#8a1c1c" });
      await chrome.action.setBadgeText({ tabId, text });
    },
  };
}

async function tabHasFillRules(tabId: number): Promise<boolean> {
  const rules = await chrome.declarativeNetRequest.getSessionRules();
  return rules.some((rule) => FILL_RULE_ID_SET.has(rule.id) && (rule.condition.tabIds ?? []).includes(tabId));
}

export async function startFillRun(tabId: number, session: FillSession, adapterId: string): Promise<ControllerView> {
  const previous = controllers.get(tabId);
  if (!mayStartOnTab(previous?.view.phase ?? null, await tabHasFillRules(tabId))) {
    const refused: ControllerView = previous?.view ?? { phase: "STOPPED", runId: null,
      reason: "QUARANTINED_TAB_CLOSE_IT", detail: {}, progress: null };
    views.set(tabId, refused);
    return refused;
  }
  const tab = await chrome.tabs.get(tabId);
  const url = new URL(tab.url!);
  const ids = await identity();
  let controller: FillRunController | null = null;
  const ports: FillPorts = {
    // 6E-A: a FILLED run's heartbeat carries the submit directives.
    onBeat: (beat) => {
      const runId = controller?.view.runId;
      if (runId && controller?.view.phase === "FILLED") {
        routeSubmitBeat({ tabId, runId, sessionId: session.sessionId, sessionToken: session.sessionToken,
                          executorInstanceId: ids.executorInstanceId, browserSessionId: ids.browserSessionId,
                          adapterId, employerHost: url.hostname, origin: url.origin, store: sessionStore }, beat);
      }
    },
    server: new HttpFillServer(session.sessionId, session.sessionToken),
    page: pagePort(tabId, adapterId, url.href, url.origin),
    quarantine: quarantinePort(tabId, url.hostname),
    browser: browserPort(tabId, url.origin),
    store: sessionStore,
  };
  controller = new FillRunController(ports, { tabId, sessionId: session.sessionId,
    sessionToken: session.sessionToken, ...ids }, (view) => views.set(tabId, view));
  controllers.set(tabId, controller);
  views.set(tabId, controller.view);
  return controller.run();
}

export function fillViewFor(tabId: number): ControllerView | null {
  return views.get(tabId) ?? null;
}

export async function fillPermissionsGranted(): Promise<boolean> {
  return chrome.permissions.contains({ permissions: [...SAFE_FILL_OPTIONAL_PERMISSIONS] });
}

export function registerFillListeners(): void {
  chrome.tabs.onRemoved.addListener((tabId) => {
    const controller = controllers.get(tabId);
    if (!controller) return;
    controllers.delete(tabId);
    views.delete(tabId);
    forgetSubmitTab(tabId);
    void controller.onTabClosed();
  });
  chrome.tabs.onCreated.addListener((tab) => {
    for (const [tabId, controller] of controllers) {
      if (isOpenerCreated(tab, tabId)) {
        void controller.onDetection("NAVIGATION_ATTEMPT_OBSERVED", { cause: "opener_created_tab" });
      }
    }
  });
  // A run is never resumed after the worker restarts (spec §11.3, §11.4).
  void recoverAfterRestart(sessionStore, (sessionId, token) => new HttpFillServer(sessionId, token));
  // 6E-A: a restarted worker never clicks again (spec J3).
  void recoverSubmits(sessionStore);
}

// A detection from the page bundle of the execution tab, for its own run.
export function routeFillDetection(message: { runId?: unknown; kind?: unknown; detail?: unknown },
                                   sender: chrome.runtime.MessageSender): boolean {
  const tabId = sender.tab?.id;
  const controller = tabId === undefined ? undefined : controllers.get(tabId);
  if (!controller || controller.view.runId !== message.runId || typeof message.kind !== "string") return false;
  void controller.onDetection(message.kind, (message.detail ?? {}) as Record<string, unknown>);
  return true;
}
