// Bundle 6D-B settle (spec §12.3): one macrotask, then a SETTLE_QUIET_PERIOD
// with no RELEVANT application-surface mutation, capped at SETTLE_CAP.
// Relevant mutation continuing through the cap is STRUCTURE_UNSTABLE; the
// executor never proceeds on timer expiry. Settling only decides when an
// action can be verified; it proves nothing about permanence.

import { SETTLE_CAP_MS, SETTLE_QUIET_PERIOD_MS } from "./constants";

export type SettleResult = "SETTLED" | "STRUCTURE_UNSTABLE";

const CONTROL_SELECTOR = "input, select, textarea, button, option, iframe, form, fieldset, label, [role], "
  + "[contenteditable], [aria-current]";
// Attributes that change an application element's meaning (required state,
// options, identity, submit/step inventory); cosmetic ones are ignored.
const RELEVANT_ATTRIBUTES = new Set(["required", "disabled", "readonly", "type", "name", "id", "form", "value",
  "multiple", "accept", "maxlength", "pattern", "min", "max", "hidden", "role", "aria-label", "aria-labelledby",
  "aria-required", "aria-current", "selected", "checked", "for", "src", "action", "style"]);

function touchesControls(nodes: NodeList): boolean {
  return Array.from(nodes).some((node) => node.nodeType === 1
    && ((node as Element).matches(CONTROL_SELECTOR) || (node as Element).querySelector(CONTROL_SELECTOR) !== null));
}

// Relevant: controls, labels/options/frames/forms added or removed, their
// defining attributes changed, or label/option/step text changed. Unrelated
// animation, clocks and analytics DOM are not.
export function isRelevantMutation(record: MutationRecord): boolean {
  if (record.type === "childList") return touchesControls(record.addedNodes) || touchesControls(record.removedNodes);
  const target = record.type === "characterData" ? record.target.parentElement : record.target as Element;
  if (!target) return false;
  if (record.type === "attributes") {
    return target.matches(CONTROL_SELECTOR) && RELEVANT_ATTRIBUTES.has(record.attributeName ?? "");
  }
  return target.closest("label, option, legend, [aria-current], [role]") !== null;
}

export function settle(root: Node, isRelevant: (record: MutationRecord) => boolean = isRelevantMutation,
                       timing: { quietMs: number; capMs: number } = { quietMs: SETTLE_QUIET_PERIOD_MS,
                         capMs: SETTLE_CAP_MS }): Promise<SettleResult> {
  const view = (root.ownerDocument ?? (root as Document)).defaultView!;
  return new Promise((resolve) => {
    let quietTimer: ReturnType<typeof setTimeout> | undefined;
    let capTimer: ReturnType<typeof setTimeout> | undefined;
    let done = false;
    const finish = (result: SettleResult) => {
      if (done) return;
      done = true;
      observer.disconnect();
      clearTimeout(quietTimer);
      clearTimeout(capTimer);
      resolve(result);
    };
    const armQuiet = () => {
      clearTimeout(quietTimer);
      quietTimer = setTimeout(() => {
        // Records still queued at the edge of the quiet period count.
        if (observer.takeRecords().some(isRelevant)) armQuiet();
        else finish("SETTLED");
      }, timing.quietMs);
    };
    const observer = new view.MutationObserver((records) => {
      if (records.some(isRelevant)) armQuiet();
    });
    // One macrotask first: the page's own reaction to the dispatched events.
    setTimeout(() => {
      observer.observe(root, { subtree: true, childList: true, attributes: true, characterData: true });
      capTimer = setTimeout(() => finish("STRUCTURE_UNSTABLE"), timing.capMs);
      armQuiet();
    }, 0);
  });
}
