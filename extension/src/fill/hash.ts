// Bundle 6D-B canonical value hash (spec §7.5). Byte-identical to Python's
// product.fill_hash.fill_value_hash; tests/fixtures/fill/hash_vectors.json
// (generated from the Python implementation) proves it.
//
// Normalization, in this exact order: NFC; then CRLF and lone CR to LF.
// The hashed envelope is Python's canonical_json of
//   {"schema": "fill-rendered-value", "schema_version": "v1", "payload": value}
// i.e. keys sorted, "," and ":" separators, non-ASCII left as raw UTF-8.
// JSON.stringify's string escaping is the same set as Python's with
// ensure_ascii=False (\" \\ \b \f \n \r \t, other controls as \u00xx
// lowercase, U+2028/U+2029 raw), which the vectors verify.

import { sha256HexSync } from "./sha256";

export const FILL_VALUE_HASH_SCHEMA = "fill-rendered-value";
export const FILL_VALUE_HASH_VERSION = "v1";

export class UnpairedSurrogateError extends Error {
  constructor() {
    super("value contains an unpaired surrogate");
    this.name = "UnpairedSurrogateError";
  }
}

function hasUnpairedSurrogate(value: string): boolean {
  for (let i = 0; i < value.length; i++) {
    const unit = value.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      i++;  // a valid pair
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return true;  // a low surrogate with no high surrogate before it
    }
  }
  return false;
}

export function normalizeFillValue(value: string): string {
  if (typeof value !== "string") throw new TypeError("fill values are strings");
  // Python's str cannot be UTF-8 encoded with a lone surrogate (it raises);
  // TextEncoder would silently substitute U+FFFD. Reject identically instead.
  if (hasUnpairedSurrogate(value)) throw new UnpairedSurrogateError();
  return value.normalize("NFC").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

export async function fillValueHash(value: string): Promise<string> {
  const normalized = normalizeFillValue(value);
  const envelope = `{"payload":${JSON.stringify(normalized)},"schema":"${FILL_VALUE_HASH_SCHEMA}",` +
    `"schema_version":"${FILL_VALUE_HASH_VERSION}"}`;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(envelope));
  return "sha256:" + [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// The synchronous twin (same normalization and envelope) for the executor's
// single-task check-then-write (spec §11.3 c.2).
export function fillValueHashSync(value: string): string {
  const normalized = normalizeFillValue(value);
  const envelope = `{"payload":${JSON.stringify(normalized)},"schema":"${FILL_VALUE_HASH_SCHEMA}",` +
    `"schema_version":"${FILL_VALUE_HASH_VERSION}"}`;
  return "sha256:" + sha256HexSync(new TextEncoder().encode(envelope));
}
