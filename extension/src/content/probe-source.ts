import type { ProbeResult } from "./probe";

// Mirrors snapshot-source.ts's globalThis bridge pattern exactly: the
// content script (content/index.ts) writes its probe result here when no
// candidate snapshot has been injected yet, and the background worker
// reads it back via a second chrome.scripting.executeScript call — no
// chrome.runtime.sendMessage round trip needed, matching how the snapshot
// itself is already handed over.
export const INJECTED_PROBE_RESULT_KEY = "__jobsearch_handoff_probe_result__";

export function readInjectedProbeResult(
  globalObj: Record<string, unknown>,
): ProbeResult | null {
  const value = globalObj[INJECTED_PROBE_RESULT_KEY];
  if (typeof value !== "object" || value === null) return null;
  return value as ProbeResult;
}
