// Bundle 7 spec §9.2: the popup pairs and signs out through the background
// worker, which alone owns the device credential.
import { describe, expect, it, vi } from "vitest";

import { attemptPairing, attemptSignOut } from "../src/popup/pairing-form";

describe("attemptPairing", () => {
  it("asks the background to pair and returns the paired account label", async () => {
    const send = vi.fn().mockResolvedValue({ ok: true, accountLabel: "a***@example.com" });
    const result = await attemptPairing(" ABCD1234EF ", send);
    expect(result).toEqual({ ok: true, accountLabel: "a***@example.com" });
    expect(send).toHaveBeenCalledWith({ type: "pair", code: "ABCD1234EF", label: "Browser extension" });
  });

  it("returns the background's message when pairing fails", async () => {
    const send = vi.fn().mockResolvedValue({ ok: false, message: "pairing code is not valid" });
    expect(await attemptPairing("BAD", send)).toEqual({ ok: false, message: "pairing code is not valid" });
  });

  it("refuses an empty code without asking the background", async () => {
    const send = vi.fn();
    expect(await attemptPairing("   ", send)).toEqual({ ok: false, message: "Enter the pairing code shown in JobSearch." });
    expect(send).not.toHaveBeenCalled();
  });

  it("reports a failed message channel as a failure", async () => {
    const send = vi.fn().mockRejectedValue(new Error("no receiver"));
    expect(await attemptPairing("CODE", send)).toEqual({ ok: false, message: "no receiver" });
  });
});

describe("attemptSignOut", () => {
  it("asks the background to sign out", async () => {
    const send = vi.fn().mockResolvedValue({ ok: true });
    await attemptSignOut(send);
    expect(send).toHaveBeenCalledWith({ type: "sign_out" });
  });
});
