// extension/src/background/index.ts
import { ChromeEventStore } from "./chrome-event-store";
import { CredentialStore } from "./credential-store";
import { ServerClient } from "./server-client";
import { DurableEventQueue } from "./event-queue";
import { MessageRouter } from "./message-router";
import { PendingContextStore } from "./pending-context-store";
import { extractValidPendingContext } from "./pending-context-validation";
import {
  associateHandoffSession,
  isActiveTabOnPendingTarget,
  type BoundSession,
} from "./session-orchestration";
import { SessionSequenceStore } from "./session-sequence-store";
import { INJECTED_SNAPSHOT_KEY } from "../content/snapshot-source";
import type { ContentScriptMessage } from "../content/messages";

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

// The currently bound handoff session, established by
// associateHandoffSession() in runAutofillOnTab below. Deliberately a
// module-level variable (not chrome.storage) — it holds only the
// short-lived session token for as long as this worker instance lives;
// a service-worker restart re-derives it from a fresh
// associateHandoffSession() call rather than resurrecting stale token
// state, since the pending context that produced it has already been
// cleared by then in the success case.
let boundSession: BoundSession | null = null;

// sendEvent needs the session token that was live at flush time. A
// closure (rather than reading the module-level `boundSession` directly
// inside ServerClient) keeps ServerClient's own dependency surface
// unaware of orchestration state — it only knows "give me a token when
// asked."
const eventQueue = new DurableEventQueue(eventStore, (event) => {
  if (!boundSession) return Promise.resolve(false);
  return serverClient.sendEvent(event, boundSession.sessionToken);
});

let router: MessageRouter | null = null;

// Lazily (re)constructs the module-level `router` so a service-worker
// restart (MV3 workers are killed after ~30s idle) never leaves `router`
// null while the content script is still emitting messages — that would
// silently drop events instead of queuing them. Resumes clientSequence
// numbering from the per-session sequence store (session-sequence-store.ts)
// when the restart is mid-session, and starts fresh at 0 when the bound
// session id has actually changed, so a stale sequence from a prior
// session is never reused (see event-queue.ts:32's sort-by-clientSequence
// ordering, which a duplicate/rollback would corrupt).
async function ensureRouter(): Promise<MessageRouter | null> {
  if (!boundSession) return null;

  if (router && router.handoffSessionId === boundSession.sessionId) {
    return router;
  }

  const startingClientSequence = await sequenceStore.get(boundSession.sessionId);
  router = new MessageRouter(eventQueue, boundSession.sessionId, startingClientSequence);
  return router;
}

async function persistClientSequence(current: MessageRouter): Promise<void> {
  await sequenceStore.set(current.handoffSessionId, current.clientSequence);
}

// TEMPORARY placeholder pending the Task 8 probe (not yet authorized):
// genuine ATS adapter detection requires a live DOM (Adapter.detect(document)
// in src/adapters/*.ts), which is content-script territory, not something
// this background-script orchestration can or should call directly. Until
// the probe step exists and supplies a real detected adapter identity, the
// generic adapter's own id/version is used as an honest "unknown/unclassified"
// placeholder rather than inventing a domain-to-adapter guess — a wrong
// static guess would silently mislabel a session's ats_adapter_id at
// start_session time, which is worse than an explicit placeholder.
const PLACEHOLDER_ADAPTER_IDENTITY = { atsAdapterId: "generic", atsAdapterVersion: "generic@1" };

export async function runAutofillOnTab(tabId: number): Promise<void> {
  const pendingContext = await pendingContextStore.peek();
  if (!pendingContext) {
    console.warn(
      "[JobSearch Handoff] no pending handoff context; click 'Apply with extension' " +
      "on the workspace page first, then click this toolbar icon on the resulting tab.",
    );
    return;
  }

  const tab = await chrome.tabs.get(tabId);
  if (!isActiveTabOnPendingTarget(tab.url, pendingContext)) {
    // Wrong tab/domain is a recoverable, retry-able state — the pending
    // context is deliberately left intact so the user can navigate to
    // the intended page and click the toolbar icon again.
    console.warn(
      "[JobSearch Handoff] active tab does not match the pending handoff target; " +
      "navigate to the intended application page first.",
    );
    return;
  }

  const targetDomain = new URL(pendingContext.targetUrl).hostname;

  let session: BoundSession;
  try {
    session = await associateHandoffSession(
      pendingContext, targetDomain, PLACEHOLDER_ADAPTER_IDENTITY, serverClient,
    );
  } catch (err) {
    // A recoverable discovery/start failure must not clear the pending
    // context — the user can retry the toolbar click without having to
    // re-click "Apply with extension" on the workspace page.
    console.warn("[JobSearch Handoff] failed to establish handoff session", err);
    return;
  }

  // Only clear the pending context once a session has been successfully
  // bound to the intended workspace/pack/domain — never before.
  await pendingContextStore.clear();
  boundSession = session;
  await ensureRouter();

  // Injection legitimately fails on restricted URLs (chrome://, the Web
  // Store, the built-in PDF viewer, etc.) — catch so one failed click
  // doesn't surface as an unhandled promise rejection.
  try {
    // Both calls deliberately omit `world`, which defaults to "ISOLATED" —
    // NOT "MAIN". The content-script bundle calls chrome.runtime.sendMessage
    // (content/index.ts), and chrome.* APIs do not exist in the MAIN world
    // (that's the whole point of the isolated world: page scripts can never
    // reach extension messaging, per spec section 17's "unreachable from
    // page scripts" requirement and this plan's own Global Constraints).
    // Both executeScript calls share the same isolated-world globalThis for
    // this tab, so the snapshot written by the first call is still visible
    // to the second call's injected bundle via INJECTED_SNAPSHOT_KEY.
    //
    // NOTE: candidate snapshot fetch/injection is Task 10's responsibility,
    // not this task's — INJECTED_SNAPSHOT_KEY is written with an empty
    // placeholder here only to preserve the existing two-step injection
    // shape until Task 10 replaces it with a real session-scoped snapshot
    // projection fetch.
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (key: string, value: unknown) => {
        (globalThis as unknown as Record<string, unknown>)[key] = value;
      },
      args: [INJECTED_SNAPSHOT_KEY, {}],
    });

    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content/index.js"],
    });
  } catch (err) {
    console.warn("[JobSearch Handoff] injection failed", err);
  }
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

  if (
    typeof message === "object" && message !== null &&
    (message as { type?: unknown }).type === "popup_run_autofill" &&
    typeof (message as { tabId?: unknown }).tabId === "number"
  ) {
    void runAutofillOnTab((message as { tabId: number }).tabId).catch(
      (err) => console.warn("[JobSearch Handoff] autofill failed", err),
    );
    return;
  }

  if (!isValidContentScriptMessage(message, sender)) return;

  // Fire-and-forget: chrome.runtime.onMessage listeners that return
  // synchronously (no sendResponse used) don't block the content script's
  // sendMessage call on this promise. Enqueue-then-flush ordering is
  // handled inside DurableEventQueue/the router; a failed flush leaves the
  // event durably queued for the next flush trigger, per event-queue.ts's
  // existing retry contract.
  void ensureRouter()
    .then((current) => {
      if (!current) return;
      return current.route(message).then(() =>
        Promise.all([eventQueue.flush(), persistClientSequence(current)]),
      );
    })
    .catch((err) => {
      console.warn("[JobSearch Handoff] relay failed", err);
    });
});
