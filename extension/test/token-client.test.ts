// Bundle 7 spec X2/X5: device credential client.
import { describe, expect, it } from "vitest";

import {
  ACCESS_KEY, DEVICE_KEY, DeviceRevokedError, TokenClient, isAllowedLocalKey, type StorageArea,
} from "../src/background/token-client";

class MemoryArea implements StorageArea {
  data: Record<string, unknown> = {};
  async get(keys: string | string[]) {
    const wanted = Array.isArray(keys) ? keys : [keys];
    return Object.fromEntries(wanted.filter((k) => k in this.data).map((k) => [k, this.data[k]]));
  }
  async set(items: Record<string, unknown>) { Object.assign(this.data, items); }
  async remove(keys: string | string[]) { for (const k of Array.isArray(keys) ? keys : [keys]) delete this.data[k]; }
  async clear() { this.data = {}; }
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const NOW = Date.parse("2026-10-01T09:00:00Z");
const later = (minutes: number) => new Date(NOW + minutes * 60_000).toISOString();

function world(handler: (url: string, body: any) => Response) {
  const local = new MemoryArea();
  const session = new MemoryArea();
  const calls: Array<{ url: string; body: any }> = [];
  const fetchImpl = (async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, body });
    return handler(url, body);
  }) as unknown as typeof fetch;
  let clock = NOW;
  const client = new TokenClient(local, session, fetchImpl, "https://app.example.test", () => clock);
  return { client, local, session, calls, advance: (ms: number) => { clock += ms; } };
}

const PAIRED = { device_id: "dev_1", access_token: "a1", access_expires_at: later(10), refresh_token: "r1",
                 account_label: "a***@example.com" };

describe("TokenClient", () => {
  it("pairs, stores the device locally and the access token in session storage", async () => {
    const w = world(() => json(201, PAIRED));
    expect(await w.client.pair(" ABCD1234EF ", "Chrome")).toBe("a***@example.com");
    expect(w.calls[0]).toEqual({ url: "https://app.example.test/api/ext/pair",
                                 body: { code: "ABCD1234EF", device_label: "Chrome" } });
    expect(w.local.data[DEVICE_KEY]).toEqual({ deviceId: "dev_1", refreshToken: "r1", accountLabel: "a***@example.com" });
    expect((w.session.data[ACCESS_KEY] as { token: string }).token).toBe("a1");
    expect(await w.client.authHeaders()).toEqual({ Authorization: "Bearer a1" });
  });

  it("refreshes once for concurrent callers when the access token is near expiry", async () => {
    let refreshes = 0;
    const w = world((url) => {
      if (url.endsWith("/api/ext/pair")) return json(201, PAIRED);
      refreshes += 1;
      return json(200, { access_token: `a${refreshes + 1}`, access_expires_at: later(30), refresh_token: `r${refreshes + 1}` });
    });
    await w.client.pair("CODE", "Chrome");
    w.advance(9.5 * 60_000);  // inside the one-minute refresh margin
    const tokens = await Promise.all([w.client.accessToken(), w.client.accessToken(), w.client.accessToken()]);
    expect(tokens).toEqual(["a2", "a2", "a2"]);
    expect(refreshes).toBe(1);
    expect((w.local.data[DEVICE_KEY] as { refreshToken: string }).refreshToken).toBe("r2");
  });

  it("a refused refresh wipes everything and tells listeners once", async () => {
    const w = world((url) => (url.endsWith("/api/ext/pair") ? json(201, PAIRED) : json(401, { error: "DEVICE_REVOKED" })));
    await w.client.pair("CODE", "Chrome");
    w.local.data["fill_executor_instance_id"] = "ex";
    let revoked = 0;
    w.client.onRevoked(() => { revoked += 1; });
    w.advance(11 * 60_000);
    await expect(w.client.accessToken()).rejects.toBeInstanceOf(DeviceRevokedError);
    expect(revoked).toBe(1);
    expect(w.local.data).toEqual({});
    expect(w.session.data).toEqual({});
  });

  it("a DEVICE_REVOKED response from any call wipes; TOKEN_EXPIRED only drops the access token", async () => {
    const w = world(() => json(201, PAIRED));
    await w.client.pair("CODE", "Chrome");
    await w.client.handleUnauthorized("TOKEN_EXPIRED");
    expect(w.session.data[ACCESS_KEY]).toBeUndefined();
    expect(w.local.data[DEVICE_KEY]).toBeDefined();
    await w.client.handleUnauthorized("DEVICE_REVOKED");
    expect(w.local.data).toEqual({});
  });

  it("sign-out revokes the device server-side and wipes both storage areas", async () => {
    const w = world((url) => (url.endsWith("/api/ext/pair") ? json(201, PAIRED) : new Response(null, { status: 204 })));
    await w.client.pair("CODE", "Chrome");
    await w.client.signOut();
    expect(w.calls.map((c) => c.url)).toContain("https://app.example.test/api/ext/devices/self/revoke");
    expect(w.local.data).toEqual({});
    expect(w.session.data).toEqual({});
  });

  it("pairing a different account first wipes the previous one", async () => {
    const w = world(() => json(201, { ...PAIRED, device_id: "dev_2", account_label: "b***@example.com" }));
    w.local.data["handoff_session_sequence:hs_old"] = 7;
    await w.client.pair("CODE", "Chrome");
    expect(Object.keys(w.local.data)).toEqual([DEVICE_KEY]);
  });

  it("a failed pairing leaves nothing stored", async () => {
    const w = world(() => json(401, { error: "PAIRING_CODE_INVALID", message: "pairing code is not valid" }));
    await expect(w.client.pair("BAD", "Chrome")).rejects.toThrow("pairing code is not valid");
    expect(w.local.data).toEqual({});
  });
});

describe("storage allowlist (spec X5)", () => {
  it("allows only the device, executor id, event queue and per-session sequences", () => {
    for (const key of ["handoff_device", "fill_executor_instance_id", "handoff_event_queue",
                       "handoff_session_sequence:hs_1"]) {
      expect(isAllowedLocalKey(key)).toBe(true);
    }
    for (const key of ["handoff_extension_credential", "cv_bytes", "profile", "handoff_access"]) {
      expect(isAllowedLocalKey(key)).toBe(false);
    }
  });
});
