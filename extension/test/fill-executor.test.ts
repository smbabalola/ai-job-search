import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";
import { greenhouseFill } from "../src/fill/certified-adapters";
import { executeAction, type Envelope, type PlanAction } from "../src/fill/executor";
import { fillValueHash, fillValueHashSync } from "../src/fill/hash";
import { observe } from "../src/fill/observer";
import { sha256HexSync } from "../src/fill/sha256";

const here = path.dirname(fileURLToPath(import.meta.url));
const URL_ = "https://boards.greenhouse.io/acme/jobs/123";
const FAST = { settleTiming: { quietMs: 5, capMs: 200 } };

function page(body: string): JSDOM {
  return new JSDOM(`<!doctype html><body><form id="application_form">${body}</form></body>`, { url: URL_ });
}

function fixture(): JSDOM {
  return new JSDOM(readFileSync(path.join(here, "fixtures", "fill-greenhouse.html"), "utf-8"), { url: URL_ });
}

async function planFor(dom: JSDOM, key: string, kind: PlanAction["action_kind"], rendered: string | null,
                       document: PlanAction["document"] = null): Promise<PlanAction> {
  const obs = await observe(dom.window.document, greenhouseFill, { canonicalUrl: URL_, origin: new URL(URL_).origin });
  const element = obs.elements.find((e) => e.page_field_key === key)!;
  return { page_field_key: key, field_fingerprint: element.field_fingerprint, action_kind: kind,
    rendered_value_hash: rendered === null ? (document ? await fillValueHash(document.sha256) : null)
      : await fillValueHash(rendered), document };
}

function envelope(action: PlanAction, value: string | null): Envelope {
  return { envelope_id: "fenv_1", action_kind: action.action_kind, rendered_value: value,
    rendered_value_hash: action.rendered_value_hash, document: action.document };
}

function input(dom: JSDOM, id: string): HTMLInputElement {
  return dom.window.document.getElementById(id) as HTMLInputElement;
}

function recordEvents(dom: JSDOM): string[] {
  const seen: string[] = [];
  for (const type of ["input", "change", "click", "keydown", "keyup", "focus", "blur", "focusin", "submit",
    "pointerdown", "mousedown"]) {
    dom.window.document.addEventListener(type, (e) => seen.push(`${type}:${(e.target as Element).id}:${e.bubbles}`),
      true);
  }
  return seen;
}

describe("SET_TEXT → DISPATCH_INPUT → DISPATCH_CHANGE", () => {
  it("writes, dispatches exactly input and change on the element, and verifies the readback", async () => {
    const dom = fixture();
    const action = await planFor(dom, "gh:notice", "WRITE", "1 month");
    const seen = recordEvents(dom);
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "1 month"), null, FAST);
    expect(out).toMatchObject({ outcome: "WRITTEN_VERIFIED", readbackHash: action.rendered_value_hash, mutated: true });
    expect(input(dom, "notice").value).toBe("1 month");
    expect(seen).toEqual(["input:notice:true", "change:notice:true"]);
  });

  it("is a no-op when the field already holds the rendered value", async () => {
    const dom = fixture();
    input(dom, "notice").value = "1 month";
    const action = await planFor(dom, "gh:notice", "WRITE", "1 month");
    const seen = recordEvents(dom);
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "1 month"), null, FAST);
    expect(out).toMatchObject({ outcome: "NOOP_ALREADY_EQUAL", mutated: false });
    expect(seen).toEqual([]);
  });

  it("never overwrites a non-blank field", async () => {
    const dom = fixture();
    input(dom, "notice").value = "3 months";
    const action = await planFor(dom, "gh:notice", "WRITE", "1 month");
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "1 month"), null, FAST);
    expect(out).toMatchObject({ outcome: "TARGET_CHANGED", detail: "not_blank", mutated: false });
    expect(input(dom, "notice").value).toBe("3 months");
  });

  it.each([
    ["readonly", (el: HTMLInputElement) => { el.readOnly = true; }],
    ["disabled", (el: HTMLInputElement) => { el.disabled = true; }],
  ])("refuses a %s control", async (_, make) => {
    const dom = fixture();
    make(input(dom, "notice"));
    const action = await planFor(dom, "gh:notice", "WRITE", "1 month");
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "1 month"), null, FAST);
    expect(out).toMatchObject({ outcome: "TARGET_CHANGED", detail: "not_editable", mutated: false });
    expect(input(dom, "notice").value).toBe("");
  });

  it("refuses a detached or changed target", async () => {
    const dom = fixture();
    const action = await planFor(dom, "gh:notice", "WRITE", "1 month");
    input(dom, "notice").remove();
    expect(await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "1 month"), null, FAST))
      .toMatchObject({ outcome: "TARGET_CHANGED", mutated: false });
    const other = fixture();
    const changed = await planFor(other, "gh:notice", "WRITE", "1 month");
    other.window.document.querySelector('label[for="notice"]')!.textContent = "Notice (weeks)";
    expect(await executeAction(other.window.document, greenhouseFill, changed, envelope(changed, "1 month"), null, FAST))
      .toMatchObject({ outcome: "TARGET_CHANGED", detail: "fingerprint" });
  });

  it("refuses an envelope whose value does not hash to the plan", async () => {
    const dom = fixture();
    const action = await planFor(dom, "gh:notice", "WRITE", "1 month");
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "2 months"), null, FAST);
    expect(out).toMatchObject({ outcome: "READBACK_MISMATCH", detail: "envelope_mismatch", mutated: false });
    expect(input(dom, "notice").value).toBe("");
  });

  it("reports a page validity failure", async () => {
    const dom = page('<label for="code">Code</label><input id="code" name="code" type="text" pattern="[0-9]+">');
    const action = await planFor(dom, "gh:code", "WRITE", "abc");
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "abc"), null, FAST);
    expect(out).toMatchObject({ outcome: "FIELD_VALIDITY_FAILED", detail: "patternMismatch", mutated: true });
  });

  it("reports a value the page reverted", async () => {
    const dom = fixture();
    input(dom, "notice").addEventListener("change", (e) => {
      (e.target as HTMLInputElement).value = "";
    });
    const action = await planFor(dom, "gh:notice", "WRITE", "1 month");
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "1 month"), null, FAST);
    expect(out).toMatchObject({ outcome: "FIELD_VALUE_REVERTED" });
  });

  it("verifies a Unicode / CRLF textarea readback (Review Focus 2)", async () => {
    const dom = fixture();
    const value = "Line one\r\nLíne two 😀 שלום\rend";
    const action = await planFor(dom, "gh:about", "WRITE", value);
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, value), null, FAST);
    expect(out).toMatchObject({ outcome: "WRITTEN_VERIFIED", readbackHash: action.rendered_value_hash });
  });
});

describe("SET_SELECT and SET_CHECKED", () => {
  it("selects the planned option and verifies it", async () => {
    const dom = fixture();
    const action = await planFor(dom, "gh:source", "WRITE", "lnk");
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "lnk"), null, FAST);
    expect(out.outcome).toBe("WRITTEN_VERIFIED");
    expect((dom.window.document.getElementById("source") as HTMLSelectElement).selectedOptions[0].text).toBe("LinkedIn");
  });

  it("sets a radio and the whole group reads back as planned", async () => {
    const dom = page('<fieldset><label><input type="radio" name="size" id="s" value="small">Small</label>'
      + '<label><input type="radio" name="size" id="l" value="large">Large</label></fieldset>');
    const action = await planFor(dom, "gh:l", "WRITE", "large");
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, "large"), null, FAST);
    expect(out.outcome).toBe("WRITTEN_VERIFIED");
    expect([input(dom, "s").checked, input(dom, "l").checked]).toEqual([false, true]);
  });
});

describe("SET_FILES_LOCAL → DISPATCH_CHANGE", () => {
  const bytes = new TextEncoder().encode("%PDF-1.4 approved cv");
  const doc = { document_version_id: "dv_1", sha256: sha256HexSync(bytes), byte_length: bytes.length,
    filename: "cv.pdf", media_type: "application/pdf" };

  function withSettableFiles(dom: JSDOM) {
    // jsdom has no DataTransfer and no settable FileList: a platform double
    // behind the same prototype descriptor the executor uses.
    const proto = dom.window.HTMLInputElement.prototype;
    const store = new WeakMap<object, unknown>();
    Object.defineProperty(proto, "files", { configurable: true, get() { return store.get(this) ?? []; },
      set(value) { store.set(this, value); } });
    return { makeFileList: (file: File) => [file] as unknown as FileList, ...FAST };
  }

  it("attaches the exact approved bytes and verifies their SHA", async () => {
    const dom = fixture();
    const options = withSettableFiles(dom);
    const action = await planFor(dom, "gh:resume", "ATTACH_LOCAL", null, doc);
    const file = new dom.window.File([bytes], "cv.pdf", { type: "application/pdf" });
    const seen = recordEvents(dom);
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, null), file, options);
    expect(out).toMatchObject({ outcome: "ATTACH_LOCAL_VERIFIED", readbackHash: await fillValueHash(doc.sha256) });
    expect(seen).toEqual(["change:resume:true"]);
  });

  it("refuses a file that is not the approved document, before setting anything", async () => {
    const dom = fixture();
    const options = withSettableFiles(dom);
    const action = await planFor(dom, "gh:resume", "ATTACH_LOCAL", null, doc);
    const wrong = new dom.window.File([new TextEncoder().encode("another cv")], "cv.pdf", { type: "application/pdf" });
    const out = await executeAction(dom.window.document, greenhouseFill, action, envelope(action, null), wrong, options);
    expect(out).toMatchObject({ outcome: "ATTACH_LOCAL_MISMATCH", mutated: false });
    expect(input(dom, "resume").files).toHaveLength(0);
  });
});

describe("OMIT and IGNORE", () => {
  it("OMIT verifies a blank field without a single mutation or event", async () => {
    const dom = fixture();
    const action = await planFor(dom, "gh:city", "OMIT", null);
    const records: MutationRecord[] = [];
    const observer = new dom.window.MutationObserver((batch) => records.push(...batch));
    observer.observe(dom.window.document, { subtree: true, childList: true, attributes: true, characterData: true });
    const seen = recordEvents(dom);
    const out = await executeAction(dom.window.document, greenhouseFill, action, null, null, FAST);
    records.push(...observer.takeRecords());
    expect(out).toMatchObject({ outcome: "OMIT_VERIFIED", mutated: false });
    expect(records).toHaveLength(0);
    expect(seen).toEqual([]);
  });

  it("an OMIT field that is not blank does not verify", async () => {
    const dom = fixture();
    input(dom, "city").value = "Paris";
    const action = await planFor(dom, "gh:city", "OMIT", null);
    expect((await executeAction(dom.window.document, greenhouseFill, action, null, null, FAST)).outcome)
      .toBe("READBACK_MISMATCH");
    expect(input(dom, "city").value).toBe("Paris");
  });

  it("IGNORE touches nothing", async () => {
    const dom = fixture();
    const action = await planFor(dom, "gh:name:authenticity_token", "IGNORE_NON_APPLICATION", null);
    expect(await executeAction(dom.window.document, greenhouseFill, action, null, null, FAST))
      .toEqual({ outcome: "IGNORE_RECORDED", mutated: false });
  });
});

describe("the synchronous hash twins", () => {
  const vectors = JSON.parse(readFileSync(path.resolve(here, "..", "..", "tests", "fixtures", "fill",
    "hash_vectors.json"), "utf8")) as { vectors: { input: string; expected?: string }[] };

  it("fillValueHashSync equals every shared Python vector", () => {
    const hashing = vectors.vectors.filter((v) => v.expected);
    expect(hashing.length).toBeGreaterThanOrEqual(20);
    for (const v of hashing) expect(fillValueHashSync(v.input)).toBe(v.expected);
  });

  it("sha256HexSync equals WebCrypto across block boundaries", async () => {
    for (const length of [0, 1, 55, 56, 63, 64, 65, 119, 120, 128, 1000]) {
      const bytes = new Uint8Array(length).map((_, i) => (i * 31 + length) & 0xff);
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      const expected = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
      expect(sha256HexSync(bytes)).toBe(expected);
    }
  });
});
