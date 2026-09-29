// 6E-A spec E13/§19: exactly one submit click exists. It is submitClick in
// submit/submit-executor.ts, reached only through the page bundle's
// clickSubmit (which re-finds the one certified control by its bound
// fingerprint), and clickSubmit is invoked only by the SubmitController
// (asserted in submit-controller.test.ts once the controller exists).
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { SRC, identifierRefs } from "./callgraph-helpers";

describe("one SUBMIT_CLICK (6E-A spec E13)", () => {
  it("submitClick is declared in submit-executor.ts and called only by the page bundle's clickSubmit", () => {
    expect(new Set(identifierRefs("submitClick"))).toEqual(new Set([
      "submit/submit-executor.ts#submitClick",  // its declaration
      "fill/page-bundle.ts#",  // the import
      "fill/page-bundle.ts#install>clickSubmit",  // the one call
    ]));
  });

  it("the submit executor is nothing but the click", () => {
    const source = readFileSync(path.join(SRC, "submit", "submit-executor.ts"), "utf-8");
    const code = source.split("\n").filter((l) => !l.trim().startsWith("//") && l.trim()).join("\n");
    expect(code).toMatch(/export function submitClick\(el: Element\): void \{\s*\(el as HTMLElement\)\.click\(\);\s*\}/);
    expect(code.match(/export /g)).toHaveLength(1);
  });

  it("clickSubmit exists only in the page API contract, the page bundle and the SubmitController's port", () => {
    const allowed = new Set(["fill/page-api.ts", "fill/page-bundle.ts", "background/submit-wiring.ts",
      "submit/submit-controller.ts"]);
    const files_ = new Set(identifierRefs("clickSubmit").map((r) => r.split("#")[0]));
    expect([...files_].filter((f) => !allowed.has(f))).toEqual([]);
  });
});

describe("the one-shot pass is armed only around the one click (6E-A condition)", () => {
  it("clickSubmit arms, clicks and disarms in a finally, and nothing else arms it", () => {
    const source = readFileSync(path.join(SRC, "fill", "page-bundle.ts"), "utf-8");
    const body = source.slice(source.indexOf("async clickSubmit("), source.indexOf("signals(adapterId"));
    const arm = body.indexOf("allowNextSubmit()");
    const click = body.indexOf("submitClick(el)");
    const disarm = body.indexOf("disarmSubmit()");
    expect(arm).toBeGreaterThan(body.indexOf("SUBMIT_CONTROL_MISSING"));  // only once the bound control exists
    expect(arm).toBeLessThan(click);
    expect(click).toBeLessThan(disarm);
    expect(body.slice(click, disarm)).toContain("finally");
    const armers = identifierRefs("allowNextSubmit").filter((r) => !r.startsWith("fill/detections.ts"));
    expect(armers).toEqual(["fill/page-bundle.ts#install>clickSubmit"]);
  });
});
