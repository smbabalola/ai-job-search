import { describe, expect, it, vi } from "vitest";
import {
  associateHandoffSession,
  chooseSessionToResume,
  filterExactMatchingSessions,
  isActiveTabOnPendingTarget,
} from "../src/background/session-orchestration";
import type { PendingHandoffContext } from "../src/background/pending-context-store";
import type { DiscoveredSession, ServerClient } from "../src/background/server-client";

const PENDING_CONTEXT: PendingHandoffContext = {
  workspaceId: "ws_1", packArtifactId: "art_1",
  targetUrl: "https://boards.greenhouse.io/acme/jobs/1", requestedAt: Date.now(),
};
const TARGET_DOMAIN = "boards.greenhouse.io";
const ADAPTER = { atsAdapterId: "generic", atsAdapterVersion: "generic@1" };

const RECENT_ACTIVITY = new Date(Date.now() - 5 * 60 * 1000).toISOString();
const RECENT_START = new Date(Date.now() - 10 * 60 * 1000).toISOString();

function session(overrides: Partial<DiscoveredSession> = {}): DiscoveredSession {
  return {
    id: "hs_1", workspace_id: "ws_1", pack_artifact_id: "art_1",
    target_domain: TARGET_DOMAIN, status: "in_progress",
    started_at: RECENT_START, last_activity_at: RECENT_ACTIVITY,
    ...overrides,
  };
}

function fakeServerClient(overrides: Partial<ServerClient> = {}): ServerClient {
  return {
    discoverSessions: vi.fn().mockResolvedValue([]),
    resumeSession: vi.fn().mockResolvedValue({ sessionToken: "should-not-be-called" }),
    startSession: vi.fn().mockResolvedValue({ id: "should-not-be-called", sessionToken: "x" }),
    ...overrides,
  } as unknown as ServerClient;
}

describe("isActiveTabOnPendingTarget", () => {
  it("accepts an active tab whose domain matches the pending target", () => {
    expect(isActiveTabOnPendingTarget(
      "https://boards.greenhouse.io/acme/jobs/1?foo=bar", PENDING_CONTEXT,
    )).toBe(true);
  });

  it("rejects an active tab on a different domain than the pending target", () => {
    expect(isActiveTabOnPendingTarget("https://jobs.lever.co/acme/1", PENDING_CONTEXT)).toBe(false);
  });

  it("rejects when the active tab has no url", () => {
    expect(isActiveTabOnPendingTarget(undefined, PENDING_CONTEXT)).toBe(false);
  });
});

describe("filterExactMatchingSessions", () => {
  const now = () => Date.parse("2026-09-13T01:00:00.000Z");

  it("keeps a session matching workspace, exact pack, and domain, within the inactivity window", () => {
    const result = filterExactMatchingSessions([session()], PENDING_CONTEXT, TARGET_DOMAIN, now);
    expect(result).toHaveLength(1);
  });

  it("rejects a session with the same workspace and domain but a different pack_artifact_id", () => {
    const result = filterExactMatchingSessions(
      [session({ pack_artifact_id: "art_DIFFERENT" })], PENDING_CONTEXT, TARGET_DOMAIN, now,
    );
    expect(result).toHaveLength(0);
  });

  it("rejects a session whose last_activity_at exceeds the 2-hour inactivity window", () => {
    const result = filterExactMatchingSessions(
      [session({ last_activity_at: "2026-09-12T22:59:00.000Z" })], // > 2h before `now`
      PENDING_CONTEXT, TARGET_DOMAIN, now,
    );
    expect(result).toHaveLength(0);
  });

  it("rejects a session that is not in_progress", () => {
    const result = filterExactMatchingSessions(
      [session({ status: "abandoned" })], PENDING_CONTEXT, TARGET_DOMAIN, now,
    );
    expect(result).toHaveLength(0);
  });

  it("rejects a session for a different workspace", () => {
    const result = filterExactMatchingSessions(
      [session({ workspace_id: "ws_OTHER" })], PENDING_CONTEXT, TARGET_DOMAIN, now,
    );
    expect(result).toHaveLength(0);
  });

  it("rejects a session for a different target domain", () => {
    const result = filterExactMatchingSessions(
      [session({ target_domain: "jobs.lever.co" })], PENDING_CONTEXT, TARGET_DOMAIN, now,
    );
    expect(result).toHaveLength(0);
  });
});

describe("chooseSessionToResume", () => {
  it("returns null when there are no matches", () => {
    expect(chooseSessionToResume([])).toBeNull();
  });

  it("returns the only match when there is exactly one", () => {
    const only = session();
    expect(chooseSessionToResume([only])).toBe(only);
  });

  it("deterministically picks the most recently active session among multiple matches", () => {
    const older = session({ id: "hs_older", last_activity_at: "2026-09-13T00:00:00.000Z" });
    const newer = session({ id: "hs_newer", last_activity_at: "2026-09-13T00:30:00.000Z" });
    expect(chooseSessionToResume([older, newer])).toBe(newer);
    expect(chooseSessionToResume([newer, older])).toBe(newer);
  });

  it("breaks ties on identical last_activity_at using started_at, then id, deterministically", () => {
    const a = session({ id: "hs_a", started_at: "2026-09-13T00:00:00.000Z" });
    const b = session({ id: "hs_b", started_at: "2026-09-13T00:00:00.000Z" });
    const resultAB = chooseSessionToResume([a, b]);
    const resultBA = chooseSessionToResume([b, a]);
    expect(resultAB).toBe(resultBA);
  });
});

describe("associateHandoffSession", () => {
  it("resumes the exact-pack-matching session when discovery finds one", async () => {
    const client = fakeServerClient({
      discoverSessions: vi.fn().mockResolvedValue([session()]),
      resumeSession: vi.fn().mockResolvedValue({ sessionToken: "tok-resumed" }),
    });

    const result = await associateHandoffSession(PENDING_CONTEXT, TARGET_DOMAIN, ADAPTER, client);

    expect(client.resumeSession).toHaveBeenCalledWith("hs_1");
    expect(client.startSession).not.toHaveBeenCalled();
    expect(result).toEqual({
      sessionId: "hs_1", sessionToken: "tok-resumed",
      workspaceId: "ws_1", packArtifactId: "art_1",
    });
  });

  it("never resumes a session for the same workspace/domain but a different pack — starts fresh instead", async () => {
    const client = fakeServerClient({
      discoverSessions: vi.fn().mockResolvedValue([session({ pack_artifact_id: "art_OLD" })]),
      startSession: vi.fn().mockResolvedValue({ id: "hs_new", sessionToken: "tok-new" }),
    });

    const result = await associateHandoffSession(PENDING_CONTEXT, TARGET_DOMAIN, ADAPTER, client);

    expect(client.resumeSession).not.toHaveBeenCalled();
    expect(client.startSession).toHaveBeenCalledWith(expect.objectContaining({
      packArtifactId: "art_1",
    }));
    expect(result.sessionId).toBe("hs_new");
  });

  it("starts a new session pinned to the exact pending packArtifactId when an expired session exists", async () => {
    const now = () => Date.parse("2026-09-13T03:00:00.000Z");
    const client = fakeServerClient({
      discoverSessions: vi.fn().mockResolvedValue(
        [session({ last_activity_at: "2026-09-13T00:00:00.000Z" })], // > 2h before `now`
      ),
      startSession: vi.fn().mockResolvedValue({ id: "hs_new", sessionToken: "tok-new" }),
    });

    const result = await associateHandoffSession(PENDING_CONTEXT, TARGET_DOMAIN, ADAPTER, client, now);

    expect(client.resumeSession).not.toHaveBeenCalled();
    expect(client.startSession).toHaveBeenCalledWith(expect.objectContaining({
      workspaceId: "ws_1", packArtifactId: "art_1", targetUrl: PENDING_CONTEXT.targetUrl,
      targetDomain: TARGET_DOMAIN, atsAdapterId: "generic", atsAdapterVersion: "generic@1",
    }));
    expect(result.sessionId).toBe("hs_new");
  });

  it("starts a new session when discovery finds nothing at all", async () => {
    const client = fakeServerClient({
      discoverSessions: vi.fn().mockResolvedValue([]),
      startSession: vi.fn().mockResolvedValue({ id: "hs_new", sessionToken: "tok-new" }),
    });

    const result = await associateHandoffSession(PENDING_CONTEXT, TARGET_DOMAIN, ADAPTER, client);

    expect(client.startSession).toHaveBeenCalled();
    expect(result.sessionId).toBe("hs_new");
  });

  it("deterministically resumes the most recently active session when multiple exact matches exist", async () => {
    const older = session({
      id: "hs_older", last_activity_at: new Date(Date.now() - 20 * 60 * 1000).toISOString(),
    });
    const newer = session({
      id: "hs_newer", last_activity_at: new Date(Date.now() - 1 * 60 * 1000).toISOString(),
    });
    const client = fakeServerClient({
      discoverSessions: vi.fn().mockResolvedValue([older, newer]),
      resumeSession: vi.fn().mockResolvedValue({ sessionToken: "tok-resumed" }),
    });

    const result = await associateHandoffSession(PENDING_CONTEXT, TARGET_DOMAIN, ADAPTER, client);

    expect(client.resumeSession).toHaveBeenCalledWith("hs_newer");
    expect(result.sessionId).toBe("hs_newer");
  });

  it("propagates a resume failure without falling back to starting a new session", async () => {
    const client = fakeServerClient({
      discoverSessions: vi.fn().mockResolvedValue([session()]),
      resumeSession: vi.fn().mockRejectedValue(new Error("failed to resume handoff session: 401")),
    });

    await expect(
      associateHandoffSession(PENDING_CONTEXT, TARGET_DOMAIN, ADAPTER, client),
    ).rejects.toThrow("failed to resume handoff session");
    expect(client.startSession).not.toHaveBeenCalled();
  });

  it("propagates a start failure", async () => {
    const client = fakeServerClient({
      discoverSessions: vi.fn().mockResolvedValue([]),
      startSession: vi.fn().mockRejectedValue(new Error("failed to start handoff session: 400")),
    });

    await expect(
      associateHandoffSession(PENDING_CONTEXT, TARGET_DOMAIN, ADAPTER, client),
    ).rejects.toThrow("failed to start handoff session");
  });
});
