import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";

import { GREENHOUSE_SUBMIT } from "../src/submit/certification";
import { detectSignals, isVisible } from "../src/submit/signals";

const BOUND = "http://127.0.0.1:8430/acme/jobs/123";
const CONFIRM = "http://127.0.0.1:8430/acme/jobs/123/confirmation";

function doc(html: string, visible: string[] = []): Document {
  const d = new JSDOM(`<!doctype html><body>${html}</body>`).window.document;
  for (const sel of visible) {
    for (const el of d.querySelectorAll(sel)) {
      (el as HTMLElement).getBoundingClientRect = () => ({ width: 10, height: 10 }) as DOMRect;
    }
  }
  return d;
}

const signals = (d: Document, url: string, rootPresent: boolean) =>
  detectSignals(d, GREENHOUSE_SUBMIT, { url, boundUrl: BOUND, confirmationUrl: CONFIRM, rootPresent });

describe("submit signals (6E-A spec §11.1, §12.1)", () => {
  it("success: the confirmation page on the certified URL", () => {
    expect(signals(doc('<div id="application_confirmation">Thanks</div>'), CONFIRM + "?x=1", false))
      .toEqual({ success: true, failure: false, challenge: false });
  });

  it("success: the in-page swap on the bound URL once the form is gone", () => {
    expect(signals(doc('<div id="application_confirmation">Thanks</div>'), BOUND, false).success).toBe(true);
  });

  it("a query string on the bound URL or the page URL does not defeat the match", () => {
    const d = doc('<div id="application_confirmation">Thanks</div>');
    expect(detectSignals(d, GREENHOUSE_SUBMIT, { url: BOUND + "?s=xhr", boundUrl: BOUND + "?s=xhr",
      confirmationUrl: CONFIRM, rootPresent: false }).success).toBe(true);
  });

  it("no success while the form is still there, or on another URL", () => {
    expect(signals(doc('<form id="application_form"></form><div id="application_confirmation"></div>'), BOUND, true)
      .success).toBe(false);
    expect(signals(doc('<div id="application_confirmation"></div>'), "http://127.0.0.1:8430/elsewhere", false)
      .success).toBe(false);
  });

  it("failure: the form is present with a visible error explanation", () => {
    const d = doc('<form id="application_form"><div id="error_explanation">Fix</div></form>', ["#error_explanation"]);
    expect(signals(d, BOUND, true)).toEqual({ success: false, failure: true, challenge: false });
    const hidden = doc('<form id="application_form"><div id="error_explanation" style="display:none">x</div></form>');
    expect(signals(hidden, BOUND, true).failure).toBe(false);
  });

  it("challenge: only a visible challenge counts (a hidden reCAPTCHA frame is not one)", () => {
    const hiddenFrame = doc('<iframe src="https://www.google.com/recaptcha/api2/bframe"></iframe>');
    expect(signals(hiddenFrame, BOUND, true).challenge).toBe(false);
    const shown = doc('<div data-submit-challenge>Verify</div>', ["[data-submit-challenge]"]);
    expect(signals(shown, BOUND, true).challenge).toBe(true);
    const titled = doc('<iframe title="reCAPTCHA Challenge"></iframe>', ["iframe"]);
    expect(signals(titled, BOUND, true).challenge).toBe(true);
  });

  it("isVisible respects display, visibility and a zero-size box", () => {
    const d = doc('<p id="a">a</p><p id="b" style="visibility:hidden">b</p><p id="c">c</p>', ["#a", "#b"]);
    expect(isVisible(d.querySelector("#a")!)).toBe(true);
    expect(isVisible(d.querySelector("#b")!)).toBe(false);
    expect(isVisible(d.querySelector("#c")!)).toBe(false);
  });
});
