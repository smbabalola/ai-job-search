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
import { buildCandidateSnapshotFromProjection } from "./snapshot-projection";
import { fetchExactPackDocument, buildAttachmentEventPayload } from "./attachment";
import { INJECTED_SNAPSHOT_KEY } from "../content/snapshot-source";
import { INJECTED_PROBE_RESULT_KEY, readInjectedProbeResult } from "../content/probe-source";
import {
  ATTACHMENT_REQUEST_KEY, ATTACHMENT_RESULT_KEY, readInjectedAttachmentResult,
  type AttachmentRequest,
} from "../content/attachment-source";
import type { ProbeResult } from "../content/probe";
import type { ContentScriptMessage } from "../content/messages";
import type { CandidateSnapshot } from "../adapters/types";

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

// Per-tab and per-session runtime state (Task 12): replaces the
// Task-9-era single module-level `boundSession`/`router` slots, which
// would silently corrupt one tab's session the moment a second tab
// associated a different one. See session-registry.ts for the full
// isolation contract this is required to uphold.
const sessionRegistry = new SessionRegistry(
  new DurableEventQueue(eventStore, (event) => {
    const token = sessionRegistry.tokenForSession(event.handoffSessionId);
    if (!token) return Promise.resolve(false);
    return serverClient.sendEvent(event, token);
  }),
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

// Runs the content bundle once with no snapshot present — content/index.ts's
// else-branch detects this and runs probePage() (detect/scan/classify only:
// no DOM writes, no candidate data, no handoff events), storing the result
// on globalThis under INJECTED_PROBE_RESULT_KEY. A second, tiny executeScript
// call reads that value back synchronously, mirroring the existing
// snapshot-source.ts globalThis-bridge pattern rather than a
// chrome.runtime.sendMessage round trip.
async function probeTab(tabId: number): Promise<ProbeResult | null> {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content/index.js"],
  });
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (key: string) => (globalThis as unknown as Record<string, unknown>)[key],
    args: [INJECTED_PROBE_RESULT_KEY],
  });
  return readInjectedProbeResult({ [INJECTED_PROBE_RESULT_KEY]: result });
}

// Injects attachment-runner.js — a separate MAIN-world bundle (needed
// because a real File write to <input type="file"> via DataTransfer is
// only observed by the page's own scripts when it happens in that same
// world, unlike every other content script in this extension, which
// runs ISOLATED) — with the request payload pre-written to globalThis,
// then reads the result back the same globalThis-bridge way probeTab
// does.
async function attachInTab(
  tabId: number, request: AttachmentRequest,
): Promise<import("../content/attachment-dom").AttachmentAttemptResult> {
  await chrome.scripting.executeScript({
    target: { tabId }, world: "MAIN",
    func: (key: string, value: unknown) => {
      (globalThis as unknown as Record<string, unknown>)[key] = value;
    },
    args: [ATTACHMENT_REQUEST_KEY, request],
  });
  await chrome.scripting.executeScript({
    target: { tabId }, world: "MAIN",
    files: ["attachment-runner/index.js"],
  });
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId }, world: "MAIN",
    func: (key: string) => (globalThis as unknown as Record<string, unknown>)[key],
    args: [ATTACHMENT_RESULT_KEY],
  });
  return readInjectedAttachmentResult({ [ATTACHMENT_RESULT_KEY]: result }) ?? {
    outcome: "write_failed", pageFieldKey: null,
  };
}

// Fetches both documents and attempts to attach each one against a
// file-upload target the probed adapter has POSITIVELY identified for
// that kind — never a guessed/generic file input, and never a widened
// ATS host permission to search more broadly for one. A missing
// compatible target is an honest, expected non-success outcome, not a
// failure to search harder. Documents are re-fetched fresh right before
// this call (never cached/reused bytes from earlier in the session).
// Every outcome (success, no-target, write failure, fetch failure) is
// reported via the existing session-token event path — never silently
// dropped, and never reported as success when it was not. This never
// submits the application: only file-input population + outcome
// events, no form submission of any kind.
async function attachDocuments(
  tabId: number, session: BoundSession, adapterId: string,
): Promise<void> {
  const router = await sessionRegistry.ensureRouterForSession(session.sessionId);

  for (const kind of ["cv", "cover_letter"] as const) {
    let outcome: "selected" | "no_compatible_target" | "write_failed" | "fetch_failed";
    let pageFieldKey: string | null = null;
    let sha256 = "";
    let filename = "";
    let mimeType = "";
    let byteLength = 0;

    try {
      const doc = await fetchExactPackDocument(BASE_URL, session.sessionId, session.sessionToken, kind);
      sha256 = doc.sha256;
      filename = doc.filename;
      mimeType = doc.mimeType;
      byteLength = doc.byteLength;

      const attemptResult = await attachInTab(tabId, {
        adapterId, kind, filename: doc.filename, mimeType: doc.mimeType,
        fileBytes: Array.from(new Uint8Array(doc.bytes)),
      });
      outcome = attemptResult.outcome;
      pageFieldKey = attemptResult.pageFieldKey;
    } catch (err) {
      console.warn(`[JobSearch Handoff] failed to fetch/attach ${kind}`, err);
      outcome = "fetch_failed";
    }

    const payload = {
      ...buildAttachmentEventPayload(
        { kind, filename, mimeType, sha256, byteLength, bytes: new ArrayBuffer(0) },
        session.packArtifactId, "application-pack-renderer.v0",
        // buildAttachmentEventPayload's own outcome vocabulary
        // ("selected" | "upload_confirmed_by_adapter" | "rejected" |
        // "unknown") is preserved unchanged; this event's OWN eventType
        // (attachment_<outcome> below) and attempt_outcome field carry
        // the more granular real result honestly, never upgrading a
        // non-success to "selected".
        outcome === "selected" ? "selected" : "unknown",
      ),
      attempt_outcome: outcome,
    };
    await router.routeEvent(`attachment_${outcome}`, payload, pageFieldKey);
  }

  await persistSequenceForSession(session.sessionId, router.clientSequence);
}

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

  let probeResult: ProbeResult | null;
  try {
    probeResult = await probeTab(tabId);
  } catch (err) {
    // Injection legitimately fails on restricted URLs (chrome://, the Web
    // Store, the built-in PDF viewer, etc.) — recoverable, pending context
    // stays intact.
    console.warn("[JobSearch Handoff] probe injection failed", err);
    return;
  }
  if (!probeResult) {
    // No adapter detected the page at all (the generic adapter's own
    // detect() only requires a single input/textarea to exist, so this
    // means the page has no form present yet, e.g. still loading) —
    // recoverable, pending context stays intact for a retry.
    console.warn("[JobSearch Handoff] no ATS adapter detected this page; nothing to apply to yet");
    return;
  }

  let session: BoundSession;
  try {
    session = await associateHandoffSession(
      pendingContext, targetDomain,
      { atsAdapterId: probeResult.atsAdapterId, atsAdapterVersion: probeResult.atsAdapterVersion },
      serverClient,
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
  sessionRegistry.bindTab(tabId, session);
  await sessionRegistry.ensureRouterForSession(session.sessionId);

  let snapshot: CandidateSnapshot;
  try {
    // Requests ONLY the normalized field types the probe found this page
    // actually needs — never the full candidate profile (design spec
    // Section 7 / this bundle's own invariant).
    const projection = await serverClient.fetchSessionSnapshot(
      session.sessionId, probeResult.normalizedFieldTypes, session.sessionToken,
    );
    snapshot = buildCandidateSnapshotFromProjection(projection);
  } catch (err) {
    console.warn("[JobSearch Handoff] failed to fetch candidate snapshot projection", err);
    return;
  }

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
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (key: string, value: unknown) => {
        (globalThis as unknown as Record<string, unknown>)[key] = value;
      },
      args: [INJECTED_SNAPSHOT_KEY, snapshot],
    });

    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content/index.js"],
    });
  } catch (err) {
    console.warn("[JobSearch Handoff] injection failed", err);
    return;
  }

  // Attachment happens only after autofill injection has been attempted
  // — never before, and never as a precondition for autofill. A failed
  // or missing attachment target never blocks or reverses the autofill
  // that already happened. This never submits the application: only
  // file-input population + outcome events, no form submission of any
  // kind (design spec / Task 11 — "Do not auto-submit the application").
  await attachDocuments(tabId, session, probeResult.atsAdapterId);
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

  // Resolved strictly by THIS tab's own bound session — never a shared
  // "current" session across tabs, so two tabs each running their own
  // handoff session never interleave into the wrong router/sequence
  // (Task 12).
  const tabId = sender.tab!.id!;
  const boundSession = sessionRegistry.sessionForTab(tabId);
  if (!boundSession) return;

  // Fire-and-forget: chrome.runtime.onMessage listeners that return
  // synchronously (no sendResponse used) don't block the content script's
  // sendMessage call on this promise. Enqueue-then-flush ordering is
  // handled inside DurableEventQueue/the router; a failed flush leaves the
  // event durably queued for the next flush trigger, per event-queue.ts's
  // existing retry contract.
  void sessionRegistry.ensureRouterForSession(boundSession.sessionId)
    .then((router) =>
      router.route(message).then(() =>
        persistSequenceForSession(boundSession.sessionId, router.clientSequence),
      ),
    )
    .catch((err) => {
      console.warn("[JobSearch Handoff] relay failed", err);
    });
});
