// Bundle 7 spec §9.2: every handoff call carries the device's bearer token;
// session-scoped calls carry the session token as well.
import { describe, expect, it, vi } from "vitest";

import { ServerClient, type DeviceAuth } from "../src/background/server-client";
import type { QueuedEvent } from "../src/background/event-queue";

function auth(overrides: Partial<DeviceAuth> = {}): DeviceAuth & { handleUnauthorized: ReturnType<typeof vi.fn> } {
  return {
    authHeaders: vi.fn().mockResolvedValue({ Authorization: "Bearer access-1" }),
    handleUnauthorized: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as DeviceAuth & { handleUnauthorized: ReturnType<typeof vi.fn> };
}

function stubFetch(response: Partial<Response> & { json?: () => Promise<unknown> }) {
  const fetchMock = vi.fn().mockResolvedValue({ clone() { return this; }, ...response });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const START = {
  handoffTicket: "v1.t1.nonce.mac", workspaceId: "ws_1", packArtifactId: "art_1",
  targetUrl: "https://boards.greenhouse.io/acme/jobs/1", targetDomain: "boards.greenhouse.io",
  atsAdapterId: "generic", atsAdapterVersion: "generic@1",
};

describe("ServerClient device-authorized calls", () => {
  it("startSession sends the bearer token and the handoff ticket", async () => {
    const fetchMock = stubFetch({ ok: true, json: async () => ({ id: "hs_1", session_token: "tok-abc" }) });
    const result = await new ServerClient(auth(), "https://app.example.test").startSession(START);
    expect(result).toEqual({ id: "hs_1", sessionToken: "tok-abc" });
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://app.example.test/api/handoff/sessions");
    expect(options.headers).toMatchObject({ Authorization: "Bearer access-1" });
    expect(options.headers).not.toHaveProperty("X-Handoff-Credential");
    expect(JSON.parse(String(options.body))).toMatchObject({ handoff_ticket: "v1.t1.nonce.mac", workspace_id: "ws_1" });
    vi.unstubAllGlobals();
  });

  it("discoverSessions and resumeSession use the bearer token", async () => {
    const fetchMock = stubFetch({ ok: true, json: async () => ({ sessions: [], session_token: "tok-rotated" }) });
    const client = new ServerClient(auth(), "https://app.example.test");
    await client.discoverSessions("ws_1", "boards.greenhouse.io");
    await client.resumeSession("hs_1");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "https://app.example.test/api/handoff/sessions/discover?workspace_id=ws_1&target_domain=boards.greenhouse.io",
      "https://app.example.test/api/handoff/sessions/hs_1/resume",
    ]);
    for (const [, options] of fetchMock.mock.calls as Array<[string, RequestInit]>) {
      expect(options.headers).toMatchObject({ Authorization: "Bearer access-1" });
    }
    vi.unstubAllGlobals();
  });

  it("a 401 is reported to the device credential with its error code", async () => {
    stubFetch({ ok: false, status: 401, json: async () => ({ error: "DEVICE_REVOKED" }) });
    const deviceAuth = auth();
    await expect(new ServerClient(deviceAuth).resumeSession("hs_1")).rejects.toThrow();
    expect(deviceAuth.handleUnauthorized).toHaveBeenCalledWith("DEVICE_REVOKED");
    vi.unstubAllGlobals();
  });

  it("an unpaired extension cannot call the server at all", async () => {
    const fetchMock = stubFetch({ ok: true, json: async () => ({}) });
    const deviceAuth = auth({ authHeaders: vi.fn().mockRejectedValue(new Error("not paired")) });
    await expect(new ServerClient(deviceAuth).startSession(START)).rejects.toThrow("not paired");
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe("ServerClient session-token-scoped calls", () => {
  const event: QueuedEvent = {
    eventId: "evt_1", clientSequence: 1, handoffSessionId: "hs_1",
    eventType: "field_observed", eventPayload: {}, observedAt: "2026-09-13T00:00:00+00:00",
  };

  it("sendEvent carries the session token and the bearer token", async () => {
    const fetchMock = stubFetch({ ok: true, json: async () => ({}) });
    await new ServerClient(auth()).sendEvent(event, "session-tok-1");
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).toMatchObject({ "X-Handoff-Session-Token": "session-tok-1", Authorization: "Bearer access-1" });
    vi.unstubAllGlobals();
  });

  it("confirmSubmission carries the session token and the bearer token", async () => {
    const fetchMock = stubFetch({ ok: true, json: async () => ({}) });
    await new ServerClient(auth()).confirmSubmission("hs_1", false, "session-tok-1");
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).toMatchObject({ "X-Handoff-Session-Token": "session-tok-1", Authorization: "Bearer access-1" });
    vi.unstubAllGlobals();
  });
});
