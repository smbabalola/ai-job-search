// Bundle 6D-B canonical JSON and hash (spec §7.5): byte-identical to Python's
// product.autonomy_contract.canonical_json / canonical_hash for the values an
// observation carries (null, booleans, safe integers, strings, arrays and
// string-keyed objects). Every string and key is NFC-normalized, keys are
// sorted by code point, separators are "," and ":", non-ASCII stays raw
// UTF-8, and JSON.stringify's string escaping equals Python's with
// ensure_ascii=False (see hash.ts). Floats are refused, as in Python.
// The server recomputes every field fingerprint, so any drift is a refusal,
// never a silent mismatch (tests/product/test_fill_observation_from_ts.py).

import { UnpairedSurrogateError } from "./hash";
import { sha256HexSync } from "./sha256";

export type Canonical = null | boolean | number | string | Canonical[] | { [key: string]: Canonical };

export class CanonicalHashError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CanonicalHashError";
  }
}

function nfc(value: string): string {
  for (let i = 0; i < value.length; i++) {
    const unit = value.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) throw new UnpairedSurrogateError();
      i++;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      throw new UnpairedSurrogateError();
    }
  }
  return value.normalize("NFC");
}

function byCodePoint(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = x[i].codePointAt(0)! - y[i].codePointAt(0)!;
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

export function canonicalJson(value: Canonical): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) throw new CanonicalHashError("only safe integers are allowed in hashed payloads");
    return String(value);
  }
  if (typeof value === "string") return JSON.stringify(nfc(value));
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (typeof value === "object") {
    const entries = new Map<string, Canonical>();
    for (const [key, item] of Object.entries(value)) {
      if (item === undefined) throw new CanonicalHashError(`undefined value for key ${key}`);
      const normalized = nfc(key);
      if (entries.has(normalized)) throw new CanonicalHashError(`keys collide after NFC normalization: ${key}`);
      entries.set(normalized, item);
    }
    const keys = [...entries.keys()].sort(byCodePoint);
    return "{" + keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(entries.get(k)!)}`).join(",") + "}";
  }
  throw new CanonicalHashError(`unsupported type in hashed payload: ${typeof value}`);
}

export async function sha256Hex(bytes: Uint8Array<ArrayBuffer> | ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function canonicalHash(schema: string, schemaVersion: string, payload: Canonical): Promise<string> {
  const envelope = canonicalJson({ schema, schema_version: schemaVersion, payload });
  return "sha256:" + await sha256Hex(new TextEncoder().encode(envelope));
}

export function canonicalHashSync(schema: string, schemaVersion: string, payload: Canonical): string {
  const envelope = canonicalJson({ schema, schema_version: schemaVersion, payload });
  return "sha256:" + sha256HexSync(new TextEncoder().encode(envelope));
}
