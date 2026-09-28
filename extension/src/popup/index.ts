import { CredentialStore } from "../background/credential-store";
import { ServerClient } from "../background/server-client";
import { attemptPairing } from "./pairing-form";
import { renderFillView } from "./fill-view";
import type { ControllerView } from "../fill/run-controller";

const credentialStore = new CredentialStore();
const serverClient = new ServerClient(() => credentialStore.get());

async function render(): Promise<void> {
  const app = document.getElementById("app");
  if (!app) return;

  const credential = await credentialStore.get();

  if (credential) {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const state = tab?.id === undefined ? null
      : await chrome.runtime.sendMessage({ type: "fill_state", tabId: tab.id })
        .catch(() => null) as { view: ControllerView | null; permissionsGranted: boolean } | null;
    app.innerHTML = `
      <p>Paired &#x2713;</p>
      <div id="fill">${renderFillView(state?.view ?? null, state?.permissionsGranted ?? false)}</div>
      <button id="run-autofill">Run autofill on this tab</button>
    `;
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
    document.getElementById("run-autofill")?.addEventListener("click", async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) return;
      chrome.runtime.sendMessage({ type: "popup_run_autofill", tabId: tab.id });
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
      const result = await attemptPairing(input.value.trim(), serverClient, credentialStore);
      if (result.ok) {
        await render();
      } else if (messageEl) {
        messageEl.textContent = result.message;
      }
    });
  }
}

void render();
