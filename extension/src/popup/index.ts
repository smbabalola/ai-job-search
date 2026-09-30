import { attemptPairing, attemptSignOut, type SendToBackground } from "./pairing-form";
import { renderFillView, renderSubmitView } from "./fill-view";
import type { SubmitView } from "../submit/submit-controller";
import type { ControllerView } from "../fill/run-controller";

const send: SendToBackground = (message) => chrome.runtime.sendMessage(message);

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
}

async function render(): Promise<void> {
  const app = document.getElementById("app");
  if (!app) return;

  const status = await send({ type: "device_status" }).catch(() => null) as
    { paired: boolean; accountLabel: string | null } | null;

  if (status?.paired) {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const state = tab?.id === undefined ? null
      : await chrome.runtime.sendMessage({ type: "fill_state", tabId: tab.id })
        .catch(() => null) as { view: ControllerView | null; submitView?: SubmitView | null;
                                permissionsGranted: boolean } | null;
    app.innerHTML = `
      <p>Signed in as <strong>${escapeHtml(status.accountLabel ?? "")}</strong>
        <button id="sign-out">Sign out</button></p>
      <div id="submit">${renderSubmitView(state?.submitView ?? null)}</div>
      <div id="fill">${renderFillView(state?.view ?? null, state?.permissionsGranted ?? false)}</div>
    `;
    document.getElementById("sign-out")?.addEventListener("click", async () => {
      await attemptSignOut(send);
      await render();
    });
    document.getElementById("cancel-submit")?.addEventListener("click", async () => {
      if (tab?.id !== undefined) await chrome.runtime.sendMessage({ type: "submit_cancel", tabId: tab.id });
      await render();
    });
    document.getElementById("enable-fill")?.addEventListener("click", async () => {
      // Requested from the popup: a user gesture is required (spec §10.1).
      await chrome.permissions.request({ permissions: ["tabs", "webNavigation"] });
      await render();
    });
    document.getElementById("start-fill")?.addEventListener("click", () => {
      if (tab?.id === undefined) return;
      chrome.runtime.sendMessage({ type: "popup_start_fill", tabId: tab.id });
      window.close();
    });
    document.getElementById("abort-fill")?.addEventListener("click", async () => {
      if (tab?.id !== undefined) await chrome.tabs.remove(tab.id);  // the only exit: close the tab
      window.close();
    });
  } else {
    app.innerHTML = `
      <p>Paste the pairing code shown on the JobSearch web app:</p>
      <input id="pairing-code" type="text" />
      <button id="pair-button">Pair</button>
      <p id="pairing-message"></p>
    `;
    document.getElementById("pair-button")?.addEventListener("click", async () => {
      const input = document.getElementById("pairing-code") as HTMLInputElement;
      const messageEl = document.getElementById("pairing-message");
      const result = await attemptPairing(input.value, send);
      if (result.ok) {
        await render();
      } else if (messageEl) {
        messageEl.textContent = result.message;
      }
    });
  }
}

void render();
