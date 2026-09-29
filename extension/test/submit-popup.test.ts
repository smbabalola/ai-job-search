import { describe, expect, it } from "vitest";

import { renderSubmitView } from "../src/popup/fill-view";
import type { SubmitPhase } from "../src/submit/submit-controller";

const view = (phase: SubmitPhase, reason: string | null = null) => ({ phase, reason, attemptId: null });

describe("popup submit states (6E-A spec §16.3)", () => {
  it("shows the spec texts", () => {
    expect(renderSubmitView(view("REVIEW_OBSERVING"))).toContain("Checking this page for your submission review");
    expect(renderSubmitView(view("SUBMITTING"))).toContain("Submission authorized — submitting…");
    expect(renderSubmitView(view("CHALLENGE"))).toContain("Verification needed: complete the check on this page");
    expect(renderSubmitView(view("SUBMITTED"))).toContain("the employer's confirmation was seen");
    expect(renderSubmitView(view("UNCLEAR"))).toContain("Outcome unclear");
    expect(renderSubmitView(view("NOT_SUBMITTED", "<b>x</b>"))).toContain("Not submitted: &#60;b&#62;x&#60;/b&#62;");
    expect(renderSubmitView(null)).toBe("");
  });

  it("offers Cancel only before the dispatch is recorded, and never a confirmation", () => {
    const phases: SubmitPhase[] = ["REVIEW_OBSERVING", "AUTHORIZED", "DISPATCHING", "SUBMITTING", "CHALLENGE",
      "SUBMITTED", "UNCLEAR", "NOT_SUBMITTED"];
    expect(phases.filter((p) => renderSubmitView(view(p)).includes('id="cancel-submit"'))).toEqual(["AUTHORIZED"]);
    // No second authorization gate: the only button in any submit state is Cancel.
    for (const p of phases) expect(renderSubmitView(view(p))).not.toMatch(/<button(?![^>]*cancel-submit)/);
  });
});
