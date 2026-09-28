import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import { afterAll, describe, expect, it } from "vitest";
import { canonicalJson } from "../src/fill/canonical";
import { certifiedAdapterFor, greenhouseFill, leverFill, type CertifiedAdapter } from "../src/fill/certified-adapters";
import { fillValueHash } from "../src/fill/hash";
import type { ObservationV1 } from "../src/fill/observation-types";
import { observe } from "../src/fill/observer";

const here = path.dirname(fileURLToPath(import.meta.url));
const GH_URL = "https://boards.greenhouse.io/acme/jobs/123";
const LEVER_URL = "https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b/apply";
const emitted: Record<string, unknown> = {};

function load(name: string, url = GH_URL): JSDOM {
  return new JSDOM(readFileSync(path.join(here, "fixtures", name), "utf-8"), { url });
}

async function observeFixture(name: string, adapter: CertifiedAdapter = greenhouseFill, url = GH_URL,
                              prepare?: (document: Document) => void): Promise<{ dom: JSDOM; obs: ObservationV1 }> {
  const dom = load(name, url);
  prepare?.(dom.window.document);
  const obs = await observe(dom.window.document, adapter, { canonicalUrl: url, origin: new URL(url).origin });
  return { dom, obs };
}

function el(obs: ObservationV1, key: string) {
  const found = obs.elements.find((e) => e.page_field_key === key);
  if (!found) throw new Error(`no element ${key}: ${obs.elements.map((e) => e.page_field_key).join(", ")}`);
  return found;
}

function attachFile(document: Document, id: string, bytes: Uint8Array<ArrayBuffer>, name: string) {
  const window = document.defaultView!;
  const file = new window.File([bytes], name, { type: "application/pdf" });
  Object.defineProperty(document.getElementById(id)!, "files", { value: [file], configurable: true });
}

const RESUME: Uint8Array<ArrayBuffer> = new TextEncoder().encode("%PDF-1.4 resume bytes");

describe("observation v1 (spec §7.1)", () => {
  it("emits the exact wire shape with fingerprints over the identity", async () => {
    const { obs } = await observeFixture("fill-greenhouse.html", greenhouseFill, GH_URL, (document) => {
      (document.getElementById("email") as HTMLInputElement).value = "ada@example.com";
      attachFile(document, "resume", RESUME, "cv.pdf");
    });
    emitted.greenhouse = obs;
    expect(Object.keys(obs).sort()).toEqual(["context", "elements", "schema_version", "submit_controls"]);
    expect(obs.context).toMatchObject({ adapter_id: "greenhouse", adapter_version: "greenhouse@2", tenant_key: "acme",
      ats_job_id: "123", application_root_found: true, multi_step_indicators: [] });
    expect(obs.elements.map((e) => e.page_field_key)).toEqual([
      "page:site-search", "gh:name:authenticity_token", "gh:email", "gh:notice", "gh:city", "gh:source", "gh:resume",
      "gh:cover_letter", "gh:about"]);
    expect(el(obs, "page:site-search")).toMatchObject({ classification: "NON_APPLICATION",
      proof: { kind: "OUTSIDE_APPLICATION_ROOT", rule: null } });
    expect(el(obs, "gh:name:authenticity_token")).toMatchObject({ classification: "NON_APPLICATION", control_kind: "hidden",
      proof: { kind: "ADAPTER_NON_APPLICATION_RULE", rule: "greenhouse.site_state_hidden@1" } });
    expect(el(obs, "gh:notice").identity).toMatchObject({ label: "What is your notice period?", maxlength: 60,
      required: true, visible: true, form_owner: "application_form" });
    expect(el(obs, "gh:source").identity.options).toEqual([
      { option_value: "", option_text: "Choose" }, { option_value: "lnk", option_text: "LinkedIn" },
      { option_value: "ref", option_text: "Referral" }]);
    expect(el(obs, "gh:resume").control_kind).toBe("file");
    expect(obs.submit_controls).toHaveLength(1);
    expect(obs.submit_controls[0].control_fingerprint).toMatch(/^sha256:[0-9a-f]{64}$/);
  });

  it("records value state as BLANK or a hash that equals the shared vector function", async () => {
    const obs = emitted.greenhouse as ObservationV1;
    expect(el(obs, "gh:notice").value_state).toEqual({ state: "BLANK" });
    expect(el(obs, "gh:email").value_state).toEqual({ state: "NONBLANK",
      current_value_hash: await fillValueHash("ada@example.com") });
    const hex = [...new Uint8Array(await crypto.subtle.digest("SHA-256", RESUME))]
      .map((b) => b.toString(16).padStart(2, "0")).join("");
    expect(el(obs, "gh:resume").value_state).toEqual({ state: "NONBLANK", current_value_hash: await fillValueHash(hex) });
    expect(JSON.stringify(obs)).not.toContain("ada@example.com");
    expect(JSON.stringify(obs)).not.toContain("token-123");
  });

  it("detects a certified adapter only when exactly one matches", async () => {
    expect(certifiedAdapterFor(load("fill-greenhouse.html").window.document)).toBe(greenhouseFill);
    expect(certifiedAdapterFor(load("fill-lever.html", LEVER_URL).window.document)).toBe(leverFill);
    const { obs } = await observeFixture("fill-lever.html", leverFill, LEVER_URL);
    emitted.lever = obs;
    expect(obs.context).toMatchObject({ adapter_id: "lever", adapter_version: "lever@2", tenant_key: "acme" });
    expect(el(obs, "lever:name:_csrf").proof).toEqual({ kind: "ADAPTER_NON_APPLICATION_RULE",
      rule: "lever.site_state_hidden@1" });
  });

  it("takes question text from aria-labelledby, placeholder and a wrapping label (Review Focus 1)", async () => {
    const { obs } = await observeFixture("fill-aria-only-label.html");
    emitted.aria_only = obs;
    expect(el(obs, "gh:start").identity.question).toBe("When can you start?");
    expect(el(obs, "gh:salary").identity.question).toBe("Expected salary");
    expect(el(obs, "gh:country").identity).toMatchObject({ label: "Country of residence",
      question: "Country of residence" });
    // Nothing names this field: no empty-question row; the server marks the
    // identity ambiguous (tests/product/test_fill_observation_from_ts.py).
    expect(el(obs, "gh:mystery").identity).toMatchObject({ label: null, question: null });
  });

  it("gives indistinguishable fields the same fingerprint and distinct keys", async () => {
    const { obs } = await observeFixture("fill-duplicate-label.html");
    emitted.duplicate = obs;
    const [a, b] = obs.elements;
    expect([a.page_field_key, b.page_field_key]).toEqual(["gh:name:reference", "gh:name:reference#2"]);
    expect(a.field_fingerprint).toBe(b.field_fingerprint);
  });

  it("reports a conditional question as not visible", async () => {
    const { obs } = await observeFixture("fill-conditional.html");
    emitted.conditional = obs;
    expect(el(obs, "gh:visa").identity.visible).toBe(false);
    expect(el(obs, "gh:email").identity.visible).toBe(true);
  });

  it("reports wizard indicators", async () => {
    const { obs } = await observeFixture("fill-wizard.html");
    emitted.wizard = obs;
    expect(obs.context.multi_step_indicators).toEqual(["NEXT_BUTTON", "STEP_INDICATOR"]);
  });

  it("reports frames and an application frame of another origin", async () => {
    const { obs } = await observeFixture("fill-cross-origin-frame.html");
    emitted.cross_origin = obs;
    expect(obs.context.frames).toEqual([
      { frame_path: "0", origin: "https://boards.greenhouse.io" },
      { frame_path: "0.0", origin: "https://forms.other-origin.test" },
      { frame_path: "0.1", origin: "https://ads.other-origin.test" }]);
    expect(el(obs, "gh:frame:0")).toMatchObject({ classification: "APPLICATION", control_kind: "custom" });
    expect(el(obs, "gh:frame:0").identity.frame_path).toBe("0.0");
  });

  it("never mutates the DOM or changes a value while observing", async () => {
    const dom = load("fill-greenhouse.html");
    const document = dom.window.document;
    (document.getElementById("email") as HTMLInputElement).value = "ada@example.com";
    const values = () => Array.from(document.querySelectorAll("input, select, textarea"))
      .map((e) => (e as HTMLInputElement).value);
    const before = values();
    const html = document.documentElement.outerHTML;
    const records: MutationRecord[] = [];
    const observer = new dom.window.MutationObserver((batch) => records.push(...batch));
    observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
    await observe(document, greenhouseFill, { canonicalUrl: GH_URL, origin: "https://boards.greenhouse.io" });
    records.push(...observer.takeRecords());
    observer.disconnect();
    expect(records).toHaveLength(0);
    expect(values()).toEqual(before);
    expect(document.documentElement.outerHTML).toBe(html);
  });

  it("canonical JSON sorts keys, NFC-normalizes and refuses floats", () => {
    expect(canonicalJson({ b: 1, a: "é" })).toBe('{"a":"é","b":1}');
    expect(() => canonicalJson({ x: 1.5 })).toThrow();
  });
});

afterAll(() => {
  // The shared cross-language fixture: validated (fingerprints recomputed)
  // by tests/product/test_fill_observation_from_ts.py.
  const out = path.resolve(here, "..", "..", "tests", "fixtures", "fill", "observation_from_ts.json");
  mkdirSync(path.dirname(out), { recursive: true });
  const expected = { resume_sha256_bytes: Buffer.from(RESUME).toString("hex") };
  writeFileSync(out, JSON.stringify({ generated_by: "extension/test/fill-observer.test.ts", expected,
    observations: emitted }, null, 2) + "\n", "utf-8");
});
