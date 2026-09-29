import { JSDOM } from "jsdom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SETTLE_CAP_MS, SETTLE_QUIET_PERIOD_MS } from "../src/fill/constants";
import { isRelevantMutation, settle, type SettleResult } from "../src/fill/settle";

function world() {
  const dom = new JSDOM('<!doctype html><body><div id="clock">0</div><form id="application_form">'
    + '<label for="a">A</label><input id="a" name="a"></form></body>');
  const document = dom.window.document;
  return { dom, document, root: document.getElementById("application_form")! };
}

function track(promise: Promise<SettleResult>) {
  const state: { result?: SettleResult } = {};
  void promise.then((r) => { state.result = r; });
  return state;
}

function addField(document: Document, root: Element, id: string) {
  const el = document.createElement("input");
  el.id = id;
  root.appendChild(el);
}

describe("settle (spec §12.3)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("uses the pinned constants", () => {
    expect([SETTLE_QUIET_PERIOD_MS, SETTLE_CAP_MS]).toEqual([50, 1000]);
  });

  it("settles after the quiet period and ignores irrelevant mutation", async () => {
    const { document } = world();
    const state = track(settle(document.body));
    await vi.advanceTimersByTimeAsync(1);  // the first macrotask
    for (let t = 0; t < 4; t++) {
      document.getElementById("clock")!.textContent = String(t);  // a clock: irrelevant
      document.getElementById("clock")!.className = `tick-${t}`;
      await vi.advanceTimersByTimeAsync(10);
    }
    expect(state.result).toBeUndefined();  // 41 ms: inside the quiet period
    await vi.advanceTimersByTimeAsync(10);
    expect(state.result).toBe("SETTLED");  // irrelevant mutation never reset it
  });

  it("a relevant mutation at 49 ms resets the quiet period", async () => {
    const { document, root } = world();
    const state = track(settle(document.body));
    await vi.advanceTimersByTimeAsync(1);  // the quiet period started at t=0
    await vi.advanceTimersByTimeAsync(48);  // t=49
    addField(document, root, "late");
    await vi.advanceTimersByTimeAsync(2);
    expect(state.result).toBeUndefined();  // the original 50 ms edge has passed, but the timer was reset
    await vi.advanceTimersByTimeAsync(47);
    expect(state.result).toBeUndefined();
    await vi.advanceTimersByTimeAsync(2);
    expect(state.result).toBe("SETTLED");
  });

  it("continuous relevant mutation is STRUCTURE_UNSTABLE at the cap, never settled", async () => {
    const { document, root } = world();
    const state = track(settle(document.body));
    await vi.advanceTimersByTimeAsync(1);
    for (let t = 0; t < 60 && state.result === undefined; t++) {
      addField(document, root, `f${t}`);
      await vi.advanceTimersByTimeAsync(20);
    }
    expect(state.result).toBe("STRUCTURE_UNSTABLE");
  });

  it("classifies relevance", () => {
    const { dom, document, root } = world();
    const records: MutationRecord[] = [];
    const observer = new dom.window.MutationObserver((batch) => records.push(...batch));
    observer.observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });
    document.getElementById("clock")!.className = "x";
    document.getElementById("a")!.toggleAttribute("required");
    addField(document, root, "b");
    records.push(...observer.takeRecords());
    expect(records.map(isRelevantMutation)).toEqual([false, true, true]);
  });
});
