import { describe, expect, it } from "vitest";
import { extractValidPendingContext } from "../src/background/pending-context-validation";

const JOBSEARCH_WEBAPP_ORIGIN = "http://127.0.0.1:8420";

const LOOPBACK_SENDER: chrome.runtime.MessageSender = {
  tab: { id: 42 } as chrome.tabs.Tab,
  url: "http://127.0.0.1:8420/workspaces/ws_1",
};

function validMessage(overrides: Record<string, unknown> = {}) {
  return {
    type: "set_pending_handoff_context",
    workspaceId: "ws_1",
    packArtifactId: "art_1",
    targetUrl: "https://boards.greenhouse.io/acme/jobs/1",
    requestedAt: Date.now(),
    ...overrides,
  };
}

function extract(message: unknown, sender: chrome.runtime.MessageSender) {
  return extractValidPendingContext(message, sender, JOBSEARCH_WEBAPP_ORIGIN);
}

describe("extractValidPendingContext", () => {
  it("accepts a well-formed message from the loopback webapp origin", () => {
    const result = extract(validMessage(), LOOPBACK_SENDER);
    expect(result).toEqual({
      workspaceId: "ws_1", packArtifactId: "art_1",
      targetUrl: "https://boards.greenhouse.io/acme/jobs/1",
      requestedAt: expect.any(Number),
    });
  });

  it("rejects a message with no sender.tab (not a real content-script sender)", () => {
    const senderWithoutTab: chrome.runtime.MessageSender = {
      url: "http://127.0.0.1:8420/workspaces/ws_1",
    };
    expect(extract(validMessage(), senderWithoutTab)).toBeNull();
  });

  it("rejects a sender whose url is not the JobSearch loopback origin", () => {
    const foreignSender: chrome.runtime.MessageSender = {
      tab: { id: 42 } as chrome.tabs.Tab,
      url: "https://boards.greenhouse.io/acme/jobs/1",
    };
    expect(extract(validMessage(), foreignSender)).toBeNull();
  });

  it("rejects a sender with no url at all", () => {
    const senderWithoutUrl: chrome.runtime.MessageSender = { tab: { id: 42 } as chrome.tabs.Tab };
    expect(extract(validMessage(), senderWithoutUrl)).toBeNull();
  });

  it("rejects a message with an unexpected extra field", () => {
    const result = extract(
      validMessage({ candidateSnapshot: { name: "should never be here" } }),
      LOOPBACK_SENDER,
    );
    expect(result).toBeNull();
  });

  it("rejects a missing/empty workspaceId", () => {
    expect(extract(validMessage({ workspaceId: "" }), LOOPBACK_SENDER)).toBeNull();
    const { workspaceId, ...withoutWorkspaceId } = validMessage();
    expect(extract(withoutWorkspaceId, LOOPBACK_SENDER)).toBeNull();
  });

  it("rejects a missing/empty packArtifactId", () => {
    expect(extract(validMessage({ packArtifactId: "" }), LOOPBACK_SENDER)).toBeNull();
  });

  it("rejects a non-http(s) targetUrl", () => {
    expect(extract(validMessage({ targetUrl: "javascript:alert(1)" }), LOOPBACK_SENDER)).toBeNull();
    expect(extract(validMessage({ targetUrl: "not-a-url" }), LOOPBACK_SENDER)).toBeNull();
  });

  it("rejects a targetUrl with no host", () => {
    expect(extract(validMessage({ targetUrl: "https://" }), LOOPBACK_SENDER)).toBeNull();
  });

  it("rejects the wrong message type", () => {
    expect(extract(validMessage({ type: "popup_run_autofill" }), LOOPBACK_SENDER)).toBeNull();
  });

  it("rejects a non-object message", () => {
    expect(extract("not an object", LOOPBACK_SENDER)).toBeNull();
    expect(extract(null, LOOPBACK_SENDER)).toBeNull();
  });

  it("never stores candidate data, credentials, or session tokens - the extracted shape has exactly four fields", () => {
    const result = extract(validMessage(), LOOPBACK_SENDER);
    expect(result && Object.keys(result).sort()).toEqual(
      ["packArtifactId", "requestedAt", "targetUrl", "workspaceId"],
    );
  });
});
