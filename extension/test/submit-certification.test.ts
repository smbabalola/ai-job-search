import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  GREENHOUSE_SUBMIT, SUBMIT_CERTIFICATIONS, certificationById, pyReEscape, resolveEgress, submitCertificationFor,
} from "../src/submit/certification";

interface Vector { name: string; origin: string; tenant_key: string; ats_job_id: string; expected: unknown }
const vectors: Vector[] = JSON.parse(
  readFileSync(resolve(__dirname, "../../tests/fixtures/submit/egress_vectors.json"), "utf8"));

describe("submit certification (6E-A spec §9)", () => {
  it("resolves every shared vector to exactly the Python bytes", () => {
    for (const v of vectors) {
      expect(resolveEgress(GREENHOUSE_SUBMIT, v.origin, v.tenant_key, v.ats_job_id), v.name).toEqual(v.expected);
    }
  });

  it("escapes bound values so a lookalike tenant never matches (Review Focus 5)", () => {
    const [e1] = resolveEgress(GREENHOUSE_SUBMIT, "https://boards.greenhouse.io", "acme.co+x", "9");
    const re = new RegExp(e1.regex);
    expect(re.test("https://boards.greenhouse.io/acme.co+x/jobs/9")).toBe(true);
    expect(re.test("https://boards.greenhouse.io/acmeXco+x/jobs/9")).toBe(false);
    expect(re.test("https://boards.greenhouse.io/acme.cooox/jobs/9")).toBe(false);
    expect(re.test("https://boards.greenhouse.io/acme.co+x/jobs/9/extra")).toBe(false);
  });

  it("escapes Python's re.escape set and nothing else", () => {
    expect(pyReEscape("a.b-c/d:e_f")).toBe("a\\.b\\-c/d:e_f");
  });

  it("has only Greenhouse, fixture-certified, no live evidence", () => {
    expect(SUBMIT_CERTIFICATIONS.map((c) => c.certificationId)).toEqual(["greenhouse@2/submit@1"]);
    expect(GREENHOUSE_SUBMIT.status).toBe("FIXTURE_CERTIFIED");
    expect(GREENHOUSE_SUBMIT.liveEvidence).toBeNull();
    expect(submitCertificationFor("lever", "lever@2")).toBeNull();
    expect(certificationById("greenhouse@2/submit@1")).toBe(GREENHOUSE_SUBMIT);
  });
});
