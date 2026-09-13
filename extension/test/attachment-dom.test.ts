import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";
import { attemptAttachment } from "../src/content/attachment-dom";
import type { Adapter } from "../src/adapters/types";

function loadDoc(html: string): Document {
  return new JSDOM(html).window.document;
}

const NOOP_ADAPTER: Adapter = {
  id: "test-adapter", version: "test@1",
  detect: () => true, scan: () => [], classify: () => {
    throw new Error("not used in this test");
  }, map: () => null,
};

describe("attemptAttachment", () => {
  it("returns no_compatible_target (not an error) when the adapter has no findAttachmentTarget at all", () => {
    const document = loadDoc("<!doctype html><html><body></body></html>");
    const file = new File(["content"], "cv.docx");
    const result = attemptAttachment(document, NOOP_ADAPTER, "cv", file);
    expect(result.outcome).toBe("no_compatible_target");
    expect(result.pageFieldKey).toBeNull();
  });

  it("returns no_compatible_target when the adapter's findAttachmentTarget returns null for this kind", () => {
    const document = loadDoc("<!doctype html><html><body></body></html>");
    const adapter: Adapter = {
      ...NOOP_ADAPTER,
      findAttachmentTarget: () => null,
    };
    const file = new File(["content"], "cv.docx");
    const result = attemptAttachment(document, adapter, "cv", file);
    expect(result.outcome).toBe("no_compatible_target");
  });

  it("never calls findAttachmentTarget for the wrong kind", () => {
    const document = loadDoc("<!doctype html><html><body></body></html>");
    const calls: string[] = [];
    const adapter: Adapter = {
      ...NOOP_ADAPTER,
      findAttachmentTarget: (_doc, kind) => {
        calls.push(kind);
        return null;
      },
    };
    attemptAttachment(document, adapter, "cover_letter", new File(["x"], "cl.docx"));
    expect(calls).toEqual(["cover_letter"]);
  });

  it("never triggers form submission as a side effect of an attachment attempt", () => {
    const document = loadDoc(
      `<!doctype html><html><body><form id="application_form">
        <button type="submit" id="submit_app">Submit</button>
      </form></body></html>`,
    );
    let submitClicks = 0;
    document.getElementById("submit_app")!.addEventListener("click", () => { submitClicks += 1; });

    const adapter: Adapter = { ...NOOP_ADAPTER, findAttachmentTarget: () => null };
    attemptAttachment(document, adapter, "cv", new File(["x"], "cv.docx"));

    expect(submitClicks).toBe(0);
  });

  // DataTransfer (needed for the real file-write path) is not
  // implemented by JSDOM — the actual write/dispatch behavior against a
  // positively-identified target is proven in the real-browser
  // Playwright suite instead (test_handoff_browser_smoke.py's
  // attachment tests), matching the same split this codebase already
  // uses for probePage's DOM-mutation proof.
});
