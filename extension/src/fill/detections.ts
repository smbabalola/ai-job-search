// Bundle 6D-B defense in depth (spec §10.6, §12.4). NOT the firewall (the
// network quarantine is): a capture-phase submit listener cancels and records
// any submit event, and pagehide or a URL/history change records a navigation
// attempt. After FILLED a MutationObserver reports relevant post-fill change.
// Each is a stop while a run is active and evidence afterwards (the server
// decides which). Every kind is reported once per page lifetime.

import { isRelevantMutation } from "./settle";

export type DetectionKind = "SUBMIT_ATTEMPT_OBSERVED" | "NAVIGATION_ATTEMPT_OBSERVED" | "POST_FILL_CHANGE_OBSERVED"
  | "EXECUTION_CONTEXT_CLOSED";

export type Report = (kind: DetectionKind, detail: Record<string, unknown>) => void;

export interface DetectionHandle {
  enablePostFill(root: Node): void;
  dispose(): void;
}

const URL_POLL_MS = 250;

export function installDetections(win: Window, report: Report): DetectionHandle {
  const reported = new Set<DetectionKind>();
  const once = (kind: DetectionKind, detail: Record<string, unknown>) => {
    if (reported.has(kind)) return;
    reported.add(kind);
    report(kind, detail);
  };
  const onSubmit = (event: Event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    const form = event.target as HTMLFormElement | null;
    once("SUBMIT_ATTEMPT_OBSERVED", { form_id: form?.id || null });
  };
  const startHref = win.location.href;
  const onUrl = () => {
    if (win.location.href !== startHref) once("NAVIGATION_ATTEMPT_OBSERVED", { cause: "url_change" });
  };
  const onPageHide = () => once("NAVIGATION_ATTEMPT_OBSERVED", { cause: "pagehide" });
  win.addEventListener("submit", onSubmit, true);
  win.addEventListener("pagehide", onPageHide);
  win.addEventListener("popstate", onUrl);
  win.addEventListener("hashchange", onUrl);
  // history.pushState from page scripts fires no event in this world.
  const poll = win.setInterval(onUrl, URL_POLL_MS);
  let post: MutationObserver | null = null;
  return {
    enablePostFill(root: Node) {
      if (post) return;
      const Observer = (win as unknown as { MutationObserver: typeof MutationObserver }).MutationObserver;
      const observer = new Observer((records: MutationRecord[]) => {
        const relevant = records.filter(isRelevantMutation).length;
        if (relevant > 0) once("POST_FILL_CHANGE_OBSERVED", { relevant_mutations: relevant });
      });
      observer.observe(root, { subtree: true, childList: true, attributes: true, characterData: true });
      post = observer;
    },
    dispose() {
      win.removeEventListener("submit", onSubmit, true);
      win.removeEventListener("pagehide", onPageHide);
      win.removeEventListener("popstate", onUrl);
      win.removeEventListener("hashchange", onUrl);
      win.clearInterval(poll);
      post?.disconnect();
    },
  };
}

// Background side: a tab the execution tab opened is a navigation attempt.
export function isOpenerCreated(tab: { openerTabId?: number }, executionTabId: number): boolean {
  return tab.openerTabId === executionTabId;
}
