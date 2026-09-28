// Bundle 6D-B sibling-context containment (spec §10.5). Invariant: exactly
// one known document context on the employer origin, the execution
// document. Any other top-level tab or any frame in ANOTHER tab on that
// origin refuses the run.
//
// Frames inside the execution tab are not siblings: Q1 quarantines every
// request of that whole tab, subframes included (spec §10.3, §10.8).
//
// What this cannot see (documented, spec §4 / §10.5): service workers and
// SharedWorkers (contained by Q2 and adapter certification), prerendered
// pages not yet activated, and incognito tabs when the extension is not
// allowed in incognito.

export type SiblingResult =
  | { ok: true }
  | { ok: false; reason: "PERMISSIONS_MISSING" }
  | { ok: false; reason: "SIBLING_EMPLOYER_CONTEXT_OPEN"; tabId: number };

export interface SiblingApi {
  permissions: { contains(p: chrome.permissions.Permissions): Promise<boolean> };
  tabs: { query(q: chrome.tabs.QueryInfo): Promise<chrome.tabs.Tab[]> };
  webNavigation: {
    getAllFrames(d: { tabId: number }): Promise<chrome.webNavigation.GetAllFrameResultDetails[] | null>;
  };
}

export const SAFE_FILL_OPTIONAL_PERMISSIONS = ["tabs", "webNavigation"] as const;

function originOf(url: string | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

function chromeApi(): SiblingApi {
  return { permissions: chrome.permissions, tabs: chrome.tabs, webNavigation: chrome.webNavigation } as unknown as SiblingApi;
}

export async function checkSiblingContainment(executionTabId: number, employerOrigin: string,
                                              api: SiblingApi = chromeApi()): Promise<SiblingResult> {
  const granted = await api.permissions.contains({ permissions: [...SAFE_FILL_OPTIONAL_PERMISSIONS] });
  if (!granted) return { ok: false, reason: "PERMISSIONS_MISSING" };

  for (const tab of await api.tabs.query({})) {
    if (tab.id === undefined || tab.id === executionTabId) continue;
    if (originOf(tab.url) === employerOrigin) {
      return { ok: false, reason: "SIBLING_EMPLOYER_CONTEXT_OPEN", tabId: tab.id };
    }
    const frames = await api.webNavigation.getAllFrames({ tabId: tab.id });
    if (frames === null) {
      // No frame evidence (e.g. a discarded tab). Only a tab whose URL is
      // known and on another origin can be cleared without it; otherwise fail closed.
      if (originOf(tab.url) === null) return { ok: false, reason: "SIBLING_EMPLOYER_CONTEXT_OPEN", tabId: tab.id };
      continue;
    }
    if (frames.some((frame) => originOf(frame.url) === employerOrigin)) {
      return { ok: false, reason: "SIBLING_EMPLOYER_CONTEXT_OPEN", tabId: tab.id };
    }
  }
  return { ok: true };
}
