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

  // Regression tests for the origin check hardening: a naive
  // sender.url.startsWith(origin + "/") is not the same as a real
  // parsed-origin comparison. These construct URLs that a plain string
  // prefix test could handle correctly by coincidence too, but pin the
  // actual mechanism (new URL(...).origin === expectedOrigin) as the
  // real boundary going forward, and cover cases a prefix check is
  // categorically the wrong tool for.
  it("rejects a sender url with the right host but wrong port (origin includes port)", () => {
    const wrongPortSender: chrome.runtime.MessageSender = {
      tab: { id: 42 } as chrome.tabs.Tab,
      url: "http://127.0.0.1:9999/workspaces/ws_1",
    };
    expect(extract(validMessage(), wrongPortSender)).toBeNull();
  });

  it("rejects a sender url with the right host but wrong scheme", () => {
    const wrongSchemeSender: chrome.runtime.MessageSender = {
      tab: { id: 42 } as chrome.tabs.Tab,
      url: "https://127.0.0.1:8420/workspaces/ws_1",
    };
    expect(extract(validMessage(), wrongSchemeSender)).toBeNull();
  });

  it("rejects a sender url containing the origin as a substring but not as its own origin", () => {
    // Not exploitable via startsWith(origin + "/") either (this doesn't
    // begin with the origin), but confirms the check is a real parsed
    // -origin comparison, not any kind of substring/contains match.
    const lookalikeSender: chrome.runtime.MessageSender = {
      tab: { id: 42 } as chrome.tabs.Tab,
      url: "https://evil.example/?redirect=http://127.0.0.1:8420/workspaces/ws_1",
    };
    expect(extract(validMessage(), lookalikeSender)).toBeNull();
  });

  it("accepts a sender url with a different path/query on the exact same origin", () => {
    const sameOriginDifferentPath: chrome.runtime.MessageSender = {
      tab: { id: 42 } as chrome.tabs.Tab,
      url: "http://127.0.0.1:8420/some/other/path?x=1",
    };
    expect(extract(validMessage(), sameOriginDifferentPath)).not.toBeNull();
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

  // Regression tests for the requestedAt hardening: the 5-minute TTL in
  // PendingContextStore.peek() computes now() - requestedAt, so a
  // malformed or implausibly-future value could defeat that comparison
  // entirely (NaN/Infinity never expire; a far-future value keeps
  // "now - requestedAt" negative indefinitely, which is always <= the
  // TTL and so never expires either).
  it("rejects a non-numeric requestedAt", () => {
    expect(extract(validMessage({ requestedAt: "not-a-number" }), LOOPBACK_SENDER)).toBeNull();
    expect(extract(validMessage({ requestedAt: null }), LOOPBACK_SENDER)).toBeNull();
  });

  it("rejects a NaN or non-finite requestedAt", () => {
    expect(extract(validMessage({ requestedAt: NaN }), LOOPBACK_SENDER)).toBeNull();
    expect(extract(validMessage({ requestedAt: Infinity }), LOOPBACK_SENDER)).toBeNull();
    expect(extract(validMessage({ requestedAt: -Infinity }), LOOPBACK_SENDER)).toBeNull();
  });

  it("rejects a requestedAt far in the future (would never expire under the 5-minute TTL)", () => {
    const farFuture = Date.now() + 365 * 24 * 60 * 60 * 1000; // one year ahead
    expect(extract(validMessage({ requestedAt: farFuture }), LOOPBACK_SENDER)).toBeNull();
  });

  it("accepts a requestedAt within ordinary clock-skew tolerance of now", () => {
    const slightlyAhead = Date.now() + 5_000; // 5s ahead, well within tolerance
    expect(extract(validMessage({ requestedAt: slightlyAhead }), LOOPBACK_SENDER)).not.toBeNull();
  });

  it("accepts a requestedAt from a few minutes in the past (still within TTL)", () => {
    const recentPast = Date.now() - 2 * 60 * 1000;
    expect(extract(validMessage({ requestedAt: recentPast }), LOOPBACK_SENDER)).not.toBeNull();
  });
});
