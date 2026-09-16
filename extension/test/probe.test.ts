import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";
import { probePage } from "../src/content/probe";
import { genericAdapter } from "../src/adapters/generic";
import { greenhouseAdapter } from "../src/adapters/greenhouse";

function loadDoc(html: string): Document {
  return new JSDOM(html).window.document;
}

const GENERIC_FIXTURE_HTML = `<!doctype html><html><body>
  <form id="application">
    <label for="f_name">Full Name</label><input id="f_name" name="name">
    <label for="f_email">Email address</label><input id="f_email" name="email">
    <label for="f_salary">Desired salary</label><input id="f_salary" name="salary">
    <label for="f_cert"><input type="checkbox" id="f_cert" name="certify"> I certify that the information above is true and correct</label>
  </form>
</body></html>`;

const GREENHOUSE_FIXTURE_HTML = `<!doctype html><html><body>
  <form id="application_form">
    <label for="gh_name">Full Name</label><input id="gh_name" name="name">
    <label for="gh_employer">Most Recent Employer</label><input id="gh_employer" name="employer">
    <label for="gh_years">Years of Experience</label><input id="gh_years" name="years">
  </form>
</body></html>`;

describe("probePage", () => {
  it("detects the matching adapter and returns needed field types without writing to the DOM", () => {
    const document = loadDoc(GENERIC_FIXTURE_HTML);
    const beforeEmail = document.querySelector<HTMLInputElement>("#f_email")!.value;

    const result = probePage(document, [genericAdapter]);

    expect(result?.atsAdapterId).toBe("generic");
    expect(result?.atsAdapterVersion).toBe("generic@1");
    expect(result?.normalizedFieldTypes).toContain("name");
    expect(result?.normalizedFieldTypes).toContain("email");

    // Zero DOM mutation: the field's value must be exactly what it was
    // before probing (never anything written by probePage itself).
    expect(document.querySelector<HTMLInputElement>("#f_email")!.value).toBe(beforeEmail);
    expect(document.querySelector<HTMLInputElement>("#f_email")!.value).toBe("");
  });

  it("returns null when no adapter detects the page", () => {
    const document = loadDoc("<!doctype html><html><body><p>no form here</p></body></html>");
    const result = probePage(document, [genericAdapter]);
    expect(result).toBeNull();
  });

  it("excludes ask fields (e.g. salary) from the returned normalizedFieldTypes", () => {
    const document = loadDoc(GENERIC_FIXTURE_HTML);
    const result = probePage(document, [genericAdapter]);
    const salaryTypes = result?.detectedFields
      .filter((f) => f.pageFieldKey === "generic:salary")
      .map((f) => f.normalizedFieldType);
    expect(salaryTypes).toEqual(["unknown"]);
    expect(result?.normalizedFieldTypes).not.toContain("unknown");
  });

  it("excludes never fields (e.g. the certification checkbox) from normalizedFieldTypes", () => {
    const document = loadDoc(GENERIC_FIXTURE_HTML);
    const result = probePage(document, [genericAdapter]);
    expect(result?.normalizedFieldTypes).not.toContain("legal_declaration");
  });

  it("still reports detected fields for ask/never behaviors, distinct from the requestable normalizedFieldTypes list", () => {
    const document = loadDoc(GENERIC_FIXTURE_HTML);
    const result = probePage(document, [genericAdapter]);
    // detectedFields carries every scanned field regardless of behavior
    // (useful for later mapping/autofill), even though normalizedFieldTypes
    // (what gets requested from the snapshot endpoint) excludes ask/never.
    expect(result?.detectedFields.length).toBeGreaterThanOrEqual(4);
  });

  it("selects the Greenhouse adapter over generic when both are supplied and the Greenhouse form is present", () => {
    const document = loadDoc(GREENHOUSE_FIXTURE_HTML);
    const result = probePage(document, [greenhouseAdapter, genericAdapter]);
    expect(result?.atsAdapterId).toBe("greenhouse");
    expect(result?.normalizedFieldTypes).toContain("employment[0].employer");
    // years_of_experience is "suggest" behavior, so it IS included (only
    // ask/never are excluded) — but map() is never called by the probe,
    // so no candidate value is ever produced for it here.
    expect(result?.normalizedFieldTypes).toContain("years_of_experience");
  });

  it("never invokes adapter.map (no candidate snapshot exists at probe time)", () => {
    const document = loadDoc(GENERIC_FIXTURE_HTML);
    let mapCalled = false;
    const spiedAdapter = {
      ...genericAdapter,
      map: (...args: Parameters<typeof genericAdapter.map>) => {
        mapCalled = true;
        return genericAdapter.map(...args);
      },
    };
    probePage(document, [spiedAdapter]);
    expect(mapCalled).toBe(false);
  });
});
