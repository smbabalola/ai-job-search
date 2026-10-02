// Bundle 6E-A background wiring: binds the SubmitController's ports to
// chrome.* for the FILLED run's own tab (scripting in the ISOLATED world,
// session DNR rules, sibling containment, storage.session for the persisted
// dispatch phase) and to the typed submit server client. The run heartbeat
// (6D-B) delivers the directives: a Submit Review re-observation request and
// an issued human SUBMIT authorization.

import { FILL_PAGE_KEY, type FillPageApi } from "../fill/page-api";
import { verifyRuleset } from "../fill/quarantine";
import type { PhaseStore } from "../fill/run-controller";
import { checkSiblingContainment } from "../fill/siblings";
import { installSubmitEgress, restoreTotal, verifySubmitEgress } from "../submit/egress";
import { HttpSubmitServer } from "../submit/server";
import {
  SubmitController, recoverSubmitAfterRestart, type AuthorizationDirective, type SubmitEgressPort,
  type SubmitPagePort, type SubmitView,
} from "../submit/submit-controller";

const PAGE_BUNDLE = "fill-page/index.js";

export interface SubmitBinding {
  tabId: number;
  runId: string;
  sessionId: string;
  sessionToken: string;
  executorInstanceId: string;
  browserSessionId: string;
  adapterId: string;
  employerHost: string;
  origin: string;
  store: PhaseStore;
}

export interface SubmitBeat {
  reobserve?: { request_id: string } | null;
  authorization?: AuthorizationDirective | null;
}

const controllers = new Map<number, { runId: string; controller: SubmitController }>();
const views = new Map<number, SubmitView>();

async function inPage<T>(tabId: number, func: (...args: never[]) => unknown, args: unknown[]): Promise<T> {
  await chrome.scripting.executeScript({ target: { tabId }, files: [PAGE_BUNDLE] });
  const [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, func, args } as never);
  return result as T;
}

type Api = Record<string, FillPageApi>;

function pagePort(b: SubmitBinding): SubmitPagePort {
  const tabId = b.tabId;
  return {
    observe: () => inPage(tabId, (key: string, id: string) => (globalThis as unknown as Api)[key]
      .observe(id, { canonicalUrl: location.href, origin: location.origin }), [FILL_PAGE_KEY, b.adapterId]),
    detected: () => inPage(tabId, (key: string) => (globalThis as unknown as Api)[key].detected(), [FILL_PAGE_KEY]),
    watchContent: () => inPage(tabId, (key: string, id: string) =>
      (globalThis as unknown as Api)[key].watchSubmitContent(id), [FILL_PAGE_KEY, b.adapterId]),
    contentChanged: () => inPage(tabId, (key: string) => (globalThis as unknown as Api)[key].contentChanged(),
      [FILL_PAGE_KEY]),
    findSubmitControl: (certificationId, fingerprint) => inPage(tabId, (key: string, c: string, f: string) =>
      (globalThis as unknown as Api)[key].findSubmitControl(c, f), [FILL_PAGE_KEY, certificationId, fingerprint]),
    clickSubmit: (certificationId, fingerprint) => inPage(tabId, (key: string, c: string, f: string) =>
      (globalThis as unknown as Api)[key].clickSubmit(c, f), [FILL_PAGE_KEY, certificationId, fingerprint]),
    signals: (certificationId, context) => inPage(tabId,
      (key: string, id: string, c: string, ctx: { boundUrl: string; confirmationUrl: string }) => {
        const api = (globalThis as unknown as Api)[key];
        return { ...api.signals(id, c, ctx), rootPresent: api.rootPresent(id) };
      }, [FILL_PAGE_KEY, b.adapterId, certificationId, context]),
  };
}

function egressPort(tabId: number, employerHost: string): SubmitEgressPort {
  return {
    verifyTotal: (hash) => verifyRuleset(hash),
    install: async (egress) => (await installSubmitEgress(tabId, employerHost, egress)).rulesetHash,
    verify: (hash) => verifySubmitEgress(hash),
    restoreTotal: (hash) => restoreTotal(hash),
  };
}

function controllerFor(b: SubmitBinding): SubmitController {
  const existing = controllers.get(b.tabId);
  if (existing && existing.runId === b.runId) return existing.controller;
  const controller = new SubmitController({
    server: new HttpSubmitServer(b.sessionId, b.sessionToken),
    page: pagePort(b),
    egress: egressPort(b.tabId, b.employerHost),
    browser: {
      siblingsContained: async () => (await checkSiblingContainment(b.tabId, b.origin)).ok,
      now: () => Date.now(),
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    },
    store: b.store,
  }, { tabId: b.tabId, runId: b.runId, sessionId: b.sessionId, sessionToken: b.sessionToken,
       executorInstanceId: b.executorInstanceId, browserSessionId: b.browserSessionId },
  (view) => views.set(b.tabId, view));
  controllers.set(b.tabId, { runId: b.runId, controller });
  return controller;
}

// Called with every run heartbeat response of a FILLED run's tab.
export function routeSubmitBeat(b: SubmitBinding, beat: SubmitBeat): void {
  if (!beat.reobserve && !beat.authorization) return;
  const controller = controllerFor(b);
  if (beat.reobserve) void controller.observeForReview(beat.reobserve.request_id);
  if (beat.authorization) void controller.run(beat.authorization);
}

export function submitViewFor(tabId: number): SubmitView | null {
  return views.get(tabId) ?? null;
}

export function cancelSubmit(tabId: number): boolean {
  return controllers.get(tabId)?.controller.cancel() ?? false;
}

export function forgetSubmitTab(tabId: number): void {
  controllers.delete(tabId);
  views.delete(tabId);
}

// A worker restart never clicks again (spec J3).
export function recoverSubmits(store: PhaseStore): Promise<void> {
  return recoverSubmitAfterRestart(store, (sessionId, token) => new HttpSubmitServer(sessionId, token),
                                   (tabId) => egressPort(tabId, ""));
}
