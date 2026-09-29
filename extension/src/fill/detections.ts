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
  // Every kind recorded so far, in order: the controller polls it before each
  // step, so a stop never depends on the async message arriving first.
  reported(): DetectionKind[];
  // 6E-A (spec E13): the human-authorized SUBMIT_CLICK is the one submit this
  // guard lets through -- a one-shot allowance armed by the SubmitController
  // immediately before the click, only after the SUBMIT egress is verified.
  allowNextSubmit(): void;
  // 6E-A (spec §10 step 8, §12): a TRUSTED input/change inside the
  // application root (a person editing) during an attempt. Page-script
  // mutations are not edits; the mandatory re-observation catches values.
  watchContent(root: Node): void;
  contentChanged(): boolean;
  dispose(): void;
}

const URL_POLL_MS = 250;

export function installDetections(win: Window, report: Report): DetectionHandle {
  const reported = new Set<DetectionKind>();
  const order: DetectionKind[] = [];
  const once = (kind: DetectionKind, detail: Record<string, unknown>) => {
    if (reported.has(kind)) return;
    reported.add(kind);
    order.push(kind);
    report(kind, detail);
  };
  let submitAllowance = false;
  let edited = false;
  let editRoot: Node | null = null;
  const onEdit = (event: Event) => {
    const NodeCtor = (win as unknown as { Node: typeof Node }).Node;
    if (event.isTrusted && editRoot !== null && event.target instanceof NodeCtor && editRoot.contains(event.target)) {
      edited = true;
    }
  };
  const onSubmit = (event: Event) => {
    if (submitAllowance) {
      submitAllowance = false;
      return;
    }
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
    reported: () => [...order],
    allowNextSubmit() {
      submitAllowance = true;
    },
    watchContent(root: Node) {
      if (editRoot === null) {
        win.addEventListener("input", onEdit, true);
        win.addEventListener("change", onEdit, true);
      }
      editRoot = root;
    },
    contentChanged: () => edited,
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
      win.removeEventListener("input", onEdit, true);
      win.removeEventListener("change", onEdit, true);
      win.clearInterval(poll);
      post?.disconnect();
    },
  };
}

// Background side: a tab the execution tab opened is a navigation attempt.
export function isOpenerCreated(tab: { openerTabId?: number }, executionTabId: number): boolean {
  return tab.openerTabId === executionTabId;
}
