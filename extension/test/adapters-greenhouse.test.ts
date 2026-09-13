import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";
import { greenhouseAdapter } from "../src/adapters/greenhouse";

function loadFixture(): Document {
  const html = readFileSync(new URL("./fixtures/greenhouse.html", import.meta.url), "utf-8");
  return new JSDOM(html).window.document;
}

describe("greenhouse adapter", () => {
  it("detects a Greenhouse-shaped application form", () => {
    expect(greenhouseAdapter.detect(loadFixture())).toBe(true);
  });

  it("autofills the unambiguous most-recent-employer field via an adapter rule", () => {
    const document = loadFixture();
    const fields = greenhouseAdapter.scan(document);
    const employer = fields.find((f) => f.labelText === "Most Recent Employer")!;
    const decision = greenhouseAdapter.classify(employer);
    expect(decision.behavior).toBe("autofill");
    expect(decision.sourceKind).toBe("adapter_rule");
  });

  it("classifies years-of-experience as suggest, never autofill", () => {
    const document = loadFixture();
    const fields = greenhouseAdapter.scan(document);
    const years = fields.find((f) => f.labelText === "Years of Experience")!;
    const decision = greenhouseAdapter.classify(years);
    expect(decision.behavior).toBe("suggest");
    expect(decision.requiresUserApproval).toBe(true);
  });

  it("classifies disability status as ask, never captured automatically", () => {
    const document = loadFixture();
    const fields = greenhouseAdapter.scan(document);
    const disability = fields.find((f) => f.labelText.includes("Disability"))!;
    expect(greenhouseAdapter.classify(disability).behavior).toBe("ask");
  });

  it("positively identifies the real resume file-upload input for kind 'cv'", () => {
    const document = loadFixture();
    const target = greenhouseAdapter.findAttachmentTarget?.(document, "cv");
    expect(target).not.toBeNull();
    expect(target?.id).toBe("job_application_resume");
    expect(target?.type).toBe("file");
  });

  it("positively identifies the real cover-letter file-upload input for kind 'cover_letter'", () => {
    const document = loadFixture();
    const target = greenhouseAdapter.findAttachmentTarget?.(document, "cover_letter");
    expect(target).not.toBeNull();
    expect(target?.id).toBe("job_application_cover_letter");
  });

  it("returns null (never a guess) when the form has no labeled file-upload input", () => {
    const document = new JSDOM(
      `<!doctype html><html><body><div id="application_form">
        <label for="x">First Name</label><input id="x">
      </div></body></html>`,
    ).window.document;
    expect(greenhouseAdapter.findAttachmentTarget?.(document, "cv")).toBeNull();
    expect(greenhouseAdapter.findAttachmentTarget?.(document, "cover_letter")).toBeNull();
  });
});
