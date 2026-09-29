import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";

import { installDetections } from "../src/fill/detections";

function page() {
  const dom = new JSDOM('<!doctype html><body><form id="application_form"><input id="q" value="a">'
    + '<button type="submit" id="s">Submit</button></form><input id="outside"></body>', { url: "http://127.0.0.1/x" });
  const reports: string[] = [];
  const handle = installDetections(dom.window as unknown as Window, (kind) => reports.push(kind));
  return { dom, doc: dom.window.document, handle, reports };
}

function submit(doc: Document): Event {
  const event = new doc.defaultView!.Event("submit", { bubbles: true, cancelable: true });
  doc.getElementById("application_form")!.dispatchEvent(event);
  return event;
}

describe("6E-A additions to the 6D-B page guards (spec E13, §10 step 7)", () => {
  it("without an allowance every submit is still cancelled and recorded (6D-B unchanged)", () => {
    const { doc, reports } = page();
    expect(submit(doc).defaultPrevented).toBe(true);
    expect(reports).toEqual(["SUBMIT_ATTEMPT_OBSERVED"]);
  });

  it("allowNextSubmit lets exactly one submit through, then the guard is back", () => {
    const { doc, handle, reports } = page();
    handle.allowNextSubmit();
    expect(submit(doc).defaultPrevented).toBe(false);
    expect(reports).toEqual([]);
    expect(submit(doc).defaultPrevented).toBe(true);
    expect(reports).toEqual(["SUBMIT_ATTEMPT_OBSERVED"]);
  });

  // A TRUSTED event cannot be forged here (isTrusted is unforgeable); the
  // positive case -- a person typing in the application form during a
  // challenge -- is proven with real input in the browser acceptance suite.
  it("the content watch ignores page-script events and events outside the application root", () => {
    const { doc, handle } = page();
    handle.watchContent(doc.getElementById("application_form")!);
    expect(handle.contentChanged()).toBe(false);
    for (const [id, type] of [["q", "input"], ["q", "change"], ["outside", "input"]]) {
      doc.getElementById(id)!.dispatchEvent(new doc.defaultView!.Event(type, { bubbles: true }));
    }
    expect(handle.contentChanged()).toBe(false);
  });
});
