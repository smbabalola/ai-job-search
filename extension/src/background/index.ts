// extension/src/background/index.ts
import { ChromeEventStore } from "./chrome-event-store";
import { CredentialStore } from "./credential-store";
import { ServerClient } from "./server-client";
import { DurableEventQueue } from "./event-queue";
import { PendingContextStore } from "./pending-context-store";
import { extractValidPendingContext } from "./pending-context-validation";
import {
  associateHandoffSession,
  isActiveTabOnPendingTarget,
  type BoundSession,
} from "./session-orchestration";
import { SessionSequenceStore } from "./session-sequence-store";
import { SessionRegistry } from "./session-registry";
import type { ContentScriptMessage } from "../content/messages";

import * as fillQuarantine from "../fill/quarantine";
import { checkSiblingContainment } from "../fill/siblings";
import {
  detectCertifiedAdapter, fillPermissionsGranted, fillViewFor, registerFillListeners, routeFillDetection,
  startFillRun,
} from "./fill-wiring";
import { cancelSubmit, submitViewFor } from "./submit-wiring";

// 6D-B Task 2 test hook: compiled only into the FILL_TEST_HOOKS build
// (scripts/build.mjs). In production __FILL_TEST_HOOKS__ is false and this
// whole branch is removed at build time.
if (__FILL_TEST_HOOKS__) {
  // The browser acceptance suite starts runs here: automated Chrome cannot
  // click the toolbar action (the user gesture behind activeTab).
  (globalThis as unknown as Record<string, unknown>).__fillTest = {
    ...fillQuarantine, checkSiblingContainment,
    startFill: (tabId: number, sessionId: string, sessionToken: string, adapterId: string) => {
      const pending = startFillRun(tabId, { sessionId, sessionToken }, adapterId);
      void pending.catch(() => undefined);
      return true;
    },
    fillView: (tabId: number) => fillViewFor(tabId),
  };
}

const BASE_URL = "http://127.0.0.1:8420";

// The one loopback origin the webapp bridge content script is allowed
// to message from — matches manifest.json's host_permissions and the
// content-bridge content_scripts "matches" entry exactly. A message
// claiming to be the webapp bridge from any other sender URL is
// rejected outright, never trusted.
const JOBSEARCH_WEBAPP_ORIGIN = "http://127.0.0.1:8420";

const credentialStore = new CredentialStore();
const eventStore = new ChromeEventStore();
const serverClient = new ServerClient(() => credentialStore.get());
const pendingContextStore = new PendingContextStore();
const sequenceStore = new SessionSequenceStore();

// Named rather than constructed anonymously inline: the real background
// orchestration below needs to call eventQueue.flush() directly at
// several trigger points (after routing a content-script event, after
// routing an attachment outcome, and after a session is newly
// associated/bound), not merely hand the queue to SessionRegistry and
// never touch it again.
const eventQueue = new DurableEventQueue(eventStore, (event) => {
  // Resolved strictly by the event's OWN handoffSessionId, never
  // "current tab" or "last used" -- an event for a session whose token
  // isn't currently available (e.g. a stale/torn-down session) simply
  // fails this send and stays durably queued; never manufactured or
  // silently dropped.
  const token = sessionRegistry.tokenForSession(event.handoffSessionId);
  if (!token) return Promise.resolve(false);
  return serverClient.sendEvent(event, token);
});

// Per-tab and per-session runtime state (Task 12): replaces the
// Task-9-era single module-level `boundSession`/`router` slots, which
// would silently corrupt one tab's session the moment a second tab
// associated a different one. See session-registry.ts for the full
// isolation contract this is required to uphold.
const sessionRegistry = new SessionRegistry(
  eventQueue,
  (handoffSessionId) => sequenceStore.get(handoffSessionId),
);

async function persistSequenceForSession(handoffSessionId: string, sequence: number): Promise<void> {
  await sequenceStore.set(handoffSessionId, sequence);
}

// Real lifecycle trigger (Task 13 residual cleanup): a tab being closed
// is the one unambiguous "this tab's session state is genuinely no
// longer usable" signal already available to the extension, with no new
// host permissions required (chrome.tabs.onRemoved needs none beyond the
// existing "scripting"/"activeTab" grants). Deliberately NOT wired to
// any submission-detection or navigation event — ordinary same-tab
// navigation during a legitimate multi-page application must never
// release this tab's session.
chrome.tabs.onRemoved.addListener((tabId) => {
  sessionRegistry.releaseTab(tabId);
});

// 6D-B safe FILL: tab close, opener-created tabs, and restart recovery (a
// run is never resumed after the worker restarts).
registerFillListeners();

// 6D-B: starts a quarantined, plan-bound FILL run on the user-activated tab.
// Same pending-context and handoff-session binding as the handoff flow; only
// a certified adapter version (greenhouse@2 / lever@2) may run.
export async function startSafeFillOnTab(tabId: number): Promise<void> {
  const pendingContext = await pendingContextStore.peek();
  if (!pendingContext) {
    console.warn("[JobSearch Handoff] no pending handoff context; open the job from JobSearch first.");
    return;
  }
  const tab = await chrome.tabs.get(tabId);
  if (!isActiveTabOnPendingTarget(tab.url, pendingContext)) {
    console.warn("[JobSearch Handoff] active tab does not match the pending handoff target.");
    return;
  }
  const adapter = await detectCertifiedAdapter(tabId).catch(() => null);
  if (!adapter) {
    console.warn("[JobSearch Handoff] no certified adapter for this page; safe FILL is not available here.");
    return;
  }
  const session = await associateHandoffSession(
    pendingContext, new URL(pendingContext.targetUrl).hostname,
    { atsAdapterId: adapter.adapterId, atsAdapterVersion: adapter.adapterVersion }, serverClient,
  );
  await pendingContextStore.clear();
  sessionRegistry.bindTab(tabId, session);
  await startFillRun(tabId, session, adapter.adapterId);
}

// Cheap defensive guard: chrome.runtime.onMessage fires for messages from
// any context in the extension, not just an injected content script. A
// future popup/options page (separate ticket) would otherwise inherit an
// unvalidated path straight through to the server. A legitimate message
// from the injected content script always has sender.tab.id set (it's
// delivered via chrome.scripting.executeScript into a real tab), so this
// never rejects the intended flow.
function isValidContentScriptMessage(
  message: unknown,
  sender: chrome.runtime.MessageSender,
): message is ContentScriptMessage {
  if (!sender.tab?.id) return false;
  if (typeof message !== "object" || message === null) return false;
  return typeof (message as { type?: unknown }).type === "string";
}

chrome.runtime.onMessage.addListener((message: unknown, sender, sendResponse) => {
  if (
    typeof message === "object" && message !== null &&
    (message as { type?: unknown }).type === "set_pending_handoff_context"
  ) {
    const context = extractValidPendingContext(message, sender, JOBSEARCH_WEBAPP_ORIGIN);
    if (!context) {
      sendResponse({ ok: false, error: "invalid pending handoff context" });
      return;
    }
    pendingContextStore
      .set(context)
      .then(() => sendResponse({ ok: true }))
      .catch((err) => {
        console.warn("[JobSearch Handoff] failed to persist pending context", err);
        sendResponse({ ok: false, error: "failed to persist pending context" });
      });
    return true; // keep the message channel open for the async sendResponse above
  }

  const typed = typeof message === "object" && message !== null ? message as { type?: unknown; tabId?: unknown } : null;
  if (typed?.type === "popup_start_fill" && typeof typed.tabId === "number") {
    void startSafeFillOnTab(typed.tabId).catch((err) => console.warn("[JobSearch Handoff] safe FILL failed", err));
    return;
  }
  if (typed?.type === "fill_state" && typeof typed.tabId === "number" && !sender.tab) {
    const tabId = typed.tabId;
    void fillPermissionsGranted().then((permissionsGranted) => sendResponse({ view: fillViewFor(tabId),
      submitView: submitViewFor(tabId), permissionsGranted }));
    return true;
  }
  if (typed?.type === "submit_cancel" && typeof typed.tabId === "number" && !sender.tab) {
    sendResponse({ cancelled: cancelSubmit(typed.tabId) });
    return;
  }
  if (typed?.type === "fill_detection") {
    routeFillDetection(message as { runId?: unknown; kind?: unknown; detail?: unknown }, sender);
    return;
  }

  if (!isValidContentScriptMessage(message, sender)) return;

  // Resolved strictly by THIS tab's own bound session — never a shared
  // "current" session across tabs, so two tabs each running their own
  // handoff session never interleave into the wrong router/sequence
  // (Task 12).
  const tabId = sender.tab!.id!;
  const boundSession = sessionRegistry.sessionForTab(tabId);
  if (!boundSession) return;

  // Fire-and-forget: chrome.runtime.onMessage listeners that return
  // synchronously (no sendResponse used) don't block the content script's
  // sendMessage call on this promise. router.route() only durably
  // enqueues (event-queue.ts's EventStore.add) -- it never sends over the
  // network itself, keeping that persistence step's contract simple and
  // synchronous-feeling from the router's perspective. The explicit
  // eventQueue.flush() call below is what actually attempts delivery
  // after this specific event is routed; DurableEventQueue.flush() is
  // itself safe to call concurrently from multiple trigger points (this
  // relay, attachDocuments, and session association) -- each call is
  // chained behind the previous one and always takes a fresh snapshot of
  // the queue when its turn arrives, so a flush requested here can never
  // miss an event enqueued moments earlier by a different trigger. A
  // failed send leaves the event durably queued (same eventId) for the
  // next flush trigger.
  void sessionRegistry.ensureRouterForSession(boundSession.sessionId)
    .then((router) =>
      router.route(message).then(() =>
        persistSequenceForSession(boundSession.sessionId, router.clientSequence),
      ),
    )
    .then(() => eventQueue.flush())
    .catch((err) => {
      console.warn("[JobSearch Handoff] relay failed", err);
    });
});
