// Bundle 7 spec X2/X5: the extension's device credential.
//
// chrome.storage.local holds ONLY the paired device (id, refresh token, masked
// account label) and the executor instance id; chrome.storage.session holds
// the 10-minute access token. Refreshes are single-flight: concurrent callers
// share one in-flight refresh, so a rotating refresh token is never presented
// twice by this device (a second presentation means theft and revokes it).
import { BACKEND_ORIGIN } from "../shared/backend";

export const DEVICE_KEY = "handoff_device";
export const ACCESS_KEY = "handoff_access";
// Keys this extension may ever keep in chrome.storage.local (spec X5):
// the device, the executor id, the durable event queue, and per-session
// client sequence counters. Nothing else (no CV bytes, profile or page data).
export const STORAGE_LOCAL_KEYS = [DEVICE_KEY, "fill_executor_instance_id", "handoff_event_queue"] as const;
export const STORAGE_LOCAL_PREFIXES = ["handoff_session_sequence:"] as const;

export function isAllowedLocalKey(key: string): boolean {
  return (STORAGE_LOCAL_KEYS as readonly string[]).includes(key)
    || STORAGE_LOCAL_PREFIXES.some((prefix) => key.startsWith(prefix));
}
const REFRESH_MARGIN_MS = 60_000;

export interface StorageArea {
  get(keys: string | string[]): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
  remove(keys: string | string[]): Promise<void>;
  clear(): Promise<void>;
}

export interface StoredDevice {
  deviceId: string;
  refreshToken: string;
  accountLabel: string;
}

interface StoredAccess {
  token: string;
  expiresAt: number;
}

export class DeviceRevokedError extends Error {
  constructor(message = "This extension is no longer paired. Pair it again from JobSearch.") {
    super(message);
    this.name = "DeviceRevokedError";
  }
}

type Fetch = typeof fetch;

export class TokenClient {
  private inflight: Promise<string> | null = null;
  private readonly revokedListeners: Array<() => void | Promise<void>> = [];

  constructor(
    private readonly local: StorageArea,
    private readonly session: StorageArea,
    private readonly fetchImpl: Fetch = (...args) => fetch(...args),
    private readonly baseUrl: string = BACKEND_ORIGIN,
    private readonly now: () => number = Date.now,
  ) {}

  onRevoked(listener: () => void | Promise<void>): void {
    this.revokedListeners.push(listener);
  }

  async device(): Promise<StoredDevice | null> {
    const stored = await this.local.get(DEVICE_KEY);
    return (stored[DEVICE_KEY] as StoredDevice | undefined) ?? null;
  }

  private async store(device: StoredDevice, access: StoredAccess): Promise<void> {
    await this.local.set({ [DEVICE_KEY]: device });
    await this.session.set({ [ACCESS_KEY]: access });
  }

  async pair(code: string, deviceLabel: string): Promise<string> {
    const response = await this.fetchImpl(`${this.baseUrl}/api/ext/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code.trim(), device_label: deviceLabel }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.message ?? `pairing failed: ${response.status}`);
    }
    // Pairing a (possibly different) account replaces everything first.
    await this.wipe();
    await this.store(
      { deviceId: body.device_id, refreshToken: body.refresh_token, accountLabel: body.account_label },
      { token: body.access_token, expiresAt: Date.parse(body.access_expires_at) },
    );
    return body.account_label as string;
  }

  async accessToken(): Promise<string> {
    const stored = (await this.session.get(ACCESS_KEY))[ACCESS_KEY] as StoredAccess | undefined;
    if (stored && stored.expiresAt - REFRESH_MARGIN_MS > this.now()) return stored.token;
    if (!this.inflight) {
      this.inflight = this.refresh().finally(() => {
        this.inflight = null;
      });
    }
    return this.inflight;
  }

  private async refresh(): Promise<string> {
    const device = await this.device();
    if (!device) throw new DeviceRevokedError("This extension is not paired yet.");
    const response = await this.fetchImpl(`${this.baseUrl}/api/ext/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: device.deviceId, refresh_token: device.refreshToken }),
    });
    const body = await response.json().catch(() => ({}));
    if (response.status === 401) {
      await this.revoked();
      throw new DeviceRevokedError();
    }
    if (!response.ok) throw new Error(`token refresh failed: ${response.status}`);
    await this.store(
      { ...device, refreshToken: body.refresh_token },
      { token: body.access_token, expiresAt: Date.parse(body.access_expires_at) },
    );
    return body.access_token as string;
  }

  async authHeaders(): Promise<Record<string, string>> {
    return { Authorization: `Bearer ${await this.accessToken()}` };
  }

  // A backend 401 on any call: an expired access token is dropped so the next
  // call refreshes; a revoked device wipes this extension.
  async handleUnauthorized(errorCode: string | undefined): Promise<void> {
    if (errorCode === "TOKEN_EXPIRED") {
      await this.session.remove(ACCESS_KEY);
    } else if (errorCode === "DEVICE_REVOKED") {
      await this.revoked();
    }
  }

  async signOut(): Promise<void> {
    try {
      const headers = await this.authHeaders();
      await this.fetchImpl(`${this.baseUrl}/api/ext/devices/self/revoke`, { method: "POST", headers });
    } catch {
      // Revocation is best effort: the local wipe below always happens.
    }
    await this.revoked();
  }

  private async revoked(): Promise<void> {
    // Listeners first: an active run restores TOTAL before its state goes.
    for (const listener of this.revokedListeners) {
      await listener();
    }
    await this.wipe();
  }

  async wipe(): Promise<void> {
    await this.local.clear();
    await this.session.clear();
  }
}
