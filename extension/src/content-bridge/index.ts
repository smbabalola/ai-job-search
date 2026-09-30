// extension/src/content-bridge/index.ts
//
// The ONLY content script scoped to the local JobSearch webapp origin
// (http://127.0.0.1:8420/*, per manifest.json — never an employer/ATS
// host). Never imported by a test (matching content/index.ts's own
// precedent) — its DOM/chrome.* wiring runs at module scope, which
// needs a real page/extension context. All actual logic lives in
// pending-context-bridge.ts, which IS unit-tested.
import { BACKEND_ORIGIN } from "../shared/backend";
import { handleApplyClick } from "./pending-context-bridge";
import type { PendingContextLaunchAck, PendingContextLaunchMessage } from "./pending-context-bridge";

function sendLaunchMessageViaRuntime(
  message: PendingContextLaunchMessage,
): Promise<PendingContextLaunchAck> {
  return chrome.runtime.sendMessage(message) as Promise<PendingContextLaunchAck>;
}

async function fetchTicketSameOrigin(workspaceId: string): Promise<string | null> {
  const token = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";
  const response = await fetch("/api/ext/handoff-tickets", {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
    body: JSON.stringify({ workspace_id: workspaceId, purpose: "HANDOFF" }),
  });
  if (!response.ok) return null;
  return ((await response.json()) as { ticket?: string }).ticket ?? null;
}

function wireUp(): void {
  // Only the configured JobSearch origin may hand the extension a launch.
  if (window.location.origin !== BACKEND_ORIGIN) return;
  document.querySelectorAll<HTMLButtonElement>(".apply-with-extension").forEach((button) => {
    button.addEventListener("click", (event) => {
      // Intercept the click entirely: the stronger invariant this
      // bridge implements is that the user is never sent to the ATS
      // unless the extension has already confirmed it durably
      // persisted the exact workspace + exact pack + exact target
      // context. That is only checkable by waiting for the
      // background worker's acknowledgement before navigating, which
      // requires owning the click's default action rather than
      // letting the button's own href/default navigation proceed.
      event.preventDefault();
      void handleApplyClick(button, sendLaunchMessageViaRuntime, fetchTicketSameOrigin).then((result) => {
        if (result.shouldNavigate && result.targetUrl) {
          window.location.assign(result.targetUrl);
        }
        // On failure: deliberately do nothing further. No navigation,
        // the button remains present and clickable, the user can
        // simply click it again.
      });
    });
  });
}

wireUp();
