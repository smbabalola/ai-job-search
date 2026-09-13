import { describe, expect, it, vi } from "vitest";
import {
  buildPendingContextMessage,
  handleApplyClick,
  isHttpUrl,
} from "../src/content-bridge/pending-context-bridge";
import type {
  PendingContextLaunchAck,
  PendingContextLaunchMessage,
} from "../src/content-bridge/pending-context-bridge";

function button(dataset: Record<string, string | undefined>) {
  return { dataset };
}

describe("buildPendingContextMessage", () => {
  it("builds a message from a button's data attributes", () => {
    const message = buildPendingContextMessage(
      button({ workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x.test/apply" }),
    );
    expect(message).toMatchObject({
      type: "set_pending_handoff_context", workspaceId: "ws_1",
      packArtifactId: "art_1", targetUrl: "https://x.test/apply",
    });
    expect(typeof message?.requestedAt).toBe("number");
  });

  it("returns null when workspaceId is missing", () => {
    expect(
      buildPendingContextMessage(button({ packArtifactId: "art_1", targetUrl: "https://x.test/apply" })),
    ).toBeNull();
  });

  it("returns null when packArtifactId is missing", () => {
    expect(
      buildPendingContextMessage(button({ workspaceId: "ws_1", targetUrl: "https://x.test/apply" })),
    ).toBeNull();
  });

  it("returns null when targetUrl is missing", () => {
    expect(
      buildPendingContextMessage(button({ workspaceId: "ws_1", packArtifactId: "art_1" })),
    ).toBeNull();
  });

  it("returns null when any attribute is an empty string", () => {
    expect(
      buildPendingContextMessage(
        button({ workspaceId: "", packArtifactId: "art_1", targetUrl: "https://x.test/apply" }),
      ),
    ).toBeNull();
  });

  it("returns null when targetUrl is not http/https", () => {
    expect(
      buildPendingContextMessage(
        button({ workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "javascript:alert(1)" }),
      ),
    ).toBeNull();
    expect(
      buildPendingContextMessage(
        button({ workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "not-a-url" }),
      ),
    ).toBeNull();
  });
});

describe("isHttpUrl", () => {
  it("accepts http/https URLs with a host", () => {
    expect(isHttpUrl("https://boards.greenhouse.io/acme/jobs/1")).toBe(true);
    expect(isHttpUrl("http://example.com/apply")).toBe(true);
  });

  it("rejects non-http(s) schemes and hostless values", () => {
    expect(isHttpUrl("javascript:alert(1)")).toBe(false);
    expect(isHttpUrl("ftp://example.com/file")).toBe(false);
    expect(isHttpUrl("not-a-url")).toBe(false);
    expect(isHttpUrl("")).toBe(false);
  });
});

describe("handleApplyClick", () => {
  const validButton = button({
    workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x.test/apply",
  });

  it("sends the exact three launch values and navigates on success", async () => {
    const sendLaunchMessage = vi.fn<
      (message: PendingContextLaunchMessage) => Promise<PendingContextLaunchAck>
    >().mockResolvedValue({ ok: true });

    const result = await handleApplyClick(validButton, sendLaunchMessage);

    expect(sendLaunchMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "set_pending_handoff_context",
        workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: "https://x.test/apply",
      }),
    );
    expect(result).toEqual({ shouldNavigate: true, targetUrl: "https://x.test/apply" });
  });

  it("does not navigate when the background reports failure", async () => {
    const sendLaunchMessage = vi.fn<
      (message: PendingContextLaunchMessage) => Promise<PendingContextLaunchAck>
    >().mockResolvedValue({ ok: false, error: "invalid pending handoff context" });

    const result = await handleApplyClick(validButton, sendLaunchMessage);

    expect(result.shouldNavigate).toBe(false);
  });

  it("does not navigate when sending the message rejects", async () => {
    const sendLaunchMessage = vi.fn<
      (message: PendingContextLaunchMessage) => Promise<PendingContextLaunchAck>
    >().mockRejectedValue(new Error("extension context invalidated"));

    const result = await handleApplyClick(validButton, sendLaunchMessage);

    expect(result.shouldNavigate).toBe(false);
  });

  it("never calls sendLaunchMessage for malformed launch context", async () => {
    const sendLaunchMessage = vi.fn<
      (message: PendingContextLaunchMessage) => Promise<PendingContextLaunchAck>
    >().mockResolvedValue({ ok: true });

    const result = await handleApplyClick(button({ workspaceId: "ws_1" }), sendLaunchMessage);

    expect(sendLaunchMessage).not.toHaveBeenCalled();
    expect(result.shouldNavigate).toBe(false);
  });
});
