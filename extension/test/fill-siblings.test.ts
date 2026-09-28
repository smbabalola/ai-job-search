import { describe, expect, it } from "vitest";
import { checkSiblingContainment, type SiblingApi } from "../src/fill/siblings";

const EMPLOYER = "https://jobs.example.test";
const EXEC_TAB = 7;

type Frame = { frameId: number; url: string };

function api(opts: {
  granted?: boolean;
  tabs: Array<{ id: number; url?: string; discarded?: boolean }>;
  frames?: Record<number, Frame[] | null>;
}): SiblingApi {
  return {
    permissions: { contains: async () => opts.granted ?? true },
    tabs: { query: async () => opts.tabs as chrome.tabs.Tab[] },
    webNavigation: {
      // An explicit null means "no frame evidence" (it must not fall back to defaults).
      getAllFrames: async ({ tabId }: { tabId: number }) =>
        (opts.frames && tabId in opts.frames
          ? opts.frames[tabId]
          : [{ frameId: 0, url: opts.tabs.find((t) => t.id === tabId)?.url ?? "" }]) as
          chrome.webNavigation.GetAllFrameResultDetails[] | null,
    },
  };
}

describe("sibling containment (spec §10.5)", () => {
  it("accepts when the execution tab is the only employer-origin context", async () => {
    const result = await checkSiblingContainment(EXEC_TAB, EMPLOYER, api({
      tabs: [{ id: EXEC_TAB, url: `${EMPLOYER}/apply` }, { id: 8, url: "https://news.example.org/" }],
    }));
    expect(result).toEqual({ ok: true });
  });

  it("refuses a second top-level tab on the employer origin", async () => {
    const result = await checkSiblingContainment(EXEC_TAB, EMPLOYER, api({
      tabs: [{ id: EXEC_TAB, url: `${EMPLOYER}/apply` }, { id: 9, url: `${EMPLOYER}/other-job` }],
    }));
    expect(result).toEqual({ ok: false, reason: "SIBLING_EMPLOYER_CONTEXT_OPEN", tabId: 9 });
  });

  it("refuses an employer-origin frame embedded in another tab", async () => {
    const result = await checkSiblingContainment(EXEC_TAB, EMPLOYER, api({
      tabs: [{ id: EXEC_TAB, url: `${EMPLOYER}/apply` }, { id: 10, url: "https://blog.example.org/" }],
      frames: { 10: [{ frameId: 0, url: "https://blog.example.org/" }, { frameId: 3, url: `${EMPLOYER}/widget` }] },
    }));
    expect(result).toEqual({ ok: false, reason: "SIBLING_EMPLOYER_CONTEXT_OPEN", tabId: 10 });
  });

  it("does not treat a different origin on the same site as a sibling", async () => {
    const result = await checkSiblingContainment(EXEC_TAB, EMPLOYER, api({
      tabs: [{ id: EXEC_TAB, url: `${EMPLOYER}/apply` }, { id: 11, url: "https://careers.example.test/" }],
    }));
    expect(result).toEqual({ ok: true });
  });

  it("does not treat frames inside the execution tab as siblings (Q1 quarantines the whole tab)", async () => {
    const result = await checkSiblingContainment(EXEC_TAB, EMPLOYER, api({
      tabs: [{ id: EXEC_TAB, url: `${EMPLOYER}/apply` }],
      frames: { [EXEC_TAB]: [{ frameId: 0, url: `${EMPLOYER}/apply` }, { frameId: 5, url: `${EMPLOYER}/embedded` }] },
    }));
    expect(result).toEqual({ ok: true });
  });

  it("fails closed on a discarded employer tab whose frames cannot be enumerated", async () => {
    const result = await checkSiblingContainment(EXEC_TAB, EMPLOYER, api({
      tabs: [{ id: EXEC_TAB, url: `${EMPLOYER}/apply` }, { id: 12, url: `${EMPLOYER}/x`, discarded: true }],
      frames: { 12: null },
    }));
    expect(result).toEqual({ ok: false, reason: "SIBLING_EMPLOYER_CONTEXT_OPEN", tabId: 12 });
  });

  it("refuses when the optional tabs/webNavigation permissions are not granted", async () => {
    const result = await checkSiblingContainment(EXEC_TAB, EMPLOYER, api({ granted: false, tabs: [] }));
    expect(result).toEqual({ ok: false, reason: "PERMISSIONS_MISSING" });
  });

  it("fails closed when another tab's URL is hidden (no enumeration evidence)", async () => {
    const result = await checkSiblingContainment(EXEC_TAB, EMPLOYER, api({
      tabs: [{ id: EXEC_TAB, url: `${EMPLOYER}/apply` }, { id: 13 }],
      frames: { 13: null },
    }));
    expect(result).toEqual({ ok: false, reason: "SIBLING_EMPLOYER_CONTEXT_OPEN", tabId: 13 });
  });
});
