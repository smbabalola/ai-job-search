import { describe, expect, it, vi } from "vitest";
import { ServerClient } from "../src/background/server-client";
import type { QueuedEvent } from "../src/background/event-queue";

describe("ServerClient.exchangePairing", () => {
  it("posts the one-time secret and returns the durable credential", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ credential_id: "extcred_1", durable_secret: "durable-abc" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => null);
    const result = await client.exchangePairing("one-time-code");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8420/api/handoff/pairing/exchange",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ one_time_secret: "one-time-code" }),
      }),
    );
    expect(result).toEqual({ credentialId: "extcred_1", durableSecret: "durable-abc" });
    vi.unstubAllGlobals();
  });

  it("throws with the server's detail message on a failed exchange", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ detail: "pairing code expired" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => null);
    await expect(client.exchangePairing("stale-code")).rejects.toThrow("pairing code expired");
    vi.unstubAllGlobals();
  });

  it("never sends an X-Handoff-Credential header for this call", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ credential_id: "extcred_1", durable_secret: "durable-abc" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => { throw new Error("should never be called"); });
    await client.exchangePairing("one-time-code");

    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).not.toHaveProperty("X-Handoff-Credential");
    vi.unstubAllGlobals();
  });
});

describe("ServerClient.startSession", () => {
  it("returns the sessionToken alongside the id", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "hs_1", session_token: "tok-abc" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => "durable-cred");
    const result = await client.startSession({
      workspaceId: "ws_1", packArtifactId: "art_1",
      targetUrl: "https://boards.greenhouse.io/acme/jobs/1",
      targetDomain: "boards.greenhouse.io",
      atsAdapterId: "generic", atsAdapterVersion: "generic@1",
    });

    expect(result).toEqual({ id: "hs_1", sessionToken: "tok-abc" });
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).toMatchObject({ "X-Handoff-Credential": "durable-cred" });
    vi.unstubAllGlobals();
  });
});

describe("ServerClient.discoverSessions", () => {
  it("GETs the discover endpoint with the durable credential and returns raw session rows", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        sessions: [
          {
            id: "hs_1", workspace_id: "ws_1", pack_artifact_id: "art_1",
            target_domain: "boards.greenhouse.io", status: "in_progress",
            started_at: "2026-09-13T00:00:00+00:00",
            last_activity_at: "2026-09-13T00:00:00+00:00",
          },
        ],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => "durable-cred");
    const result = await client.discoverSessions("ws_1", "boards.greenhouse.io");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8420/api/handoff/sessions/discover?workspace_id=ws_1&target_domain=boards.greenhouse.io",
      expect.objectContaining({ method: "GET" }),
    );
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).toMatchObject({ "X-Handoff-Credential": "durable-cred" });
    expect(result).toEqual([
      expect.objectContaining({
        id: "hs_1", pack_artifact_id: "art_1", target_domain: "boards.greenhouse.io",
        status: "in_progress",
      }),
    ]);
    vi.unstubAllGlobals();
  });

  it("throws when the discover request fails", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => "durable-cred");
    await expect(client.discoverSessions("ws_1", "boards.greenhouse.io")).rejects.toThrow();
    vi.unstubAllGlobals();
  });
});

describe("ServerClient.resumeSession", () => {
  it("POSTs to the resume endpoint with the durable credential and returns a rotated sessionToken", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ session_token: "tok-rotated" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => "durable-cred");
    const result = await client.resumeSession("hs_1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8420/api/handoff/sessions/hs_1/resume",
      expect.objectContaining({ method: "POST" }),
    );
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).toMatchObject({ "X-Handoff-Credential": "durable-cred" });
    expect(result).toEqual({ sessionToken: "tok-rotated" });
    vi.unstubAllGlobals();
  });

  it("throws when resume fails (e.g. session expired or pack mismatch enforced server-side)", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 401 });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => "durable-cred");
    await expect(client.resumeSession("hs_1")).rejects.toThrow();
    vi.unstubAllGlobals();
  });
});

describe("ServerClient session-token-scoped calls", () => {
  const event: QueuedEvent = {
    eventId: "evt_1", clientSequence: 1, handoffSessionId: "hs_1",
    eventType: "field_observed", eventPayload: {}, observedAt: "2026-09-13T00:00:00+00:00",
  };

  it("sendEvent authorizes with X-Handoff-Session-Token, not the durable credential", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => {
      throw new Error("sendEvent must not read the durable credential");
    });
    await client.sendEvent(event, "session-tok-1");

    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).toMatchObject({ "X-Handoff-Session-Token": "session-tok-1" });
    expect(options.headers).not.toHaveProperty("X-Handoff-Credential");
    vi.unstubAllGlobals();
  });

  it("confirmSubmission authorizes with X-Handoff-Session-Token, not the durable credential", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => {
      throw new Error("confirmSubmission must not read the durable credential");
    });
    await client.confirmSubmission("hs_1", false, "session-tok-1");

    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).toMatchObject({ "X-Handoff-Session-Token": "session-tok-1" });
    expect(options.headers).not.toHaveProperty("X-Handoff-Credential");
    vi.unstubAllGlobals();
  });

  it("fetchSessionSnapshot authorizes with X-Handoff-Session-Token, requests only the given field types, and returns the raw projection", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        snapshot: { name: { value: "Ada Lovelace", profile_evidence_ids: ["c1"] } },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => {
      throw new Error("fetchSessionSnapshot must not read the durable credential");
    });
    const result = await client.fetchSessionSnapshot("hs_1", ["name", "email"], "session-tok-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8420/api/handoff/sessions/hs_1/snapshot",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ normalized_field_types: ["name", "email"] }),
      }),
    );
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.headers).toMatchObject({ "X-Handoff-Session-Token": "session-tok-1" });
    expect(options.headers).not.toHaveProperty("X-Handoff-Credential");
    expect(result).toEqual({ name: { value: "Ada Lovelace", profile_evidence_ids: ["c1"] } });
    vi.unstubAllGlobals();
  });

  it("fetchSessionSnapshot throws when the request fails (e.g. wrong/expired session token)", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 401 });
    vi.stubGlobal("fetch", fetchMock);

    const client = new ServerClient(async () => "durable-cred");
    await expect(
      client.fetchSessionSnapshot("hs_1", ["name"], "bad-token"),
    ).rejects.toThrow();
    vi.unstubAllGlobals();
  });
});
