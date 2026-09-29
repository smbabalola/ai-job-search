import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import type { ObservationV1 } from "../src/fill/observation-types";
import { structureFingerprint } from "../src/fill/observer";
import { observationFingerprint } from "../src/submit/fingerprint";

const vector = JSON.parse(readFileSync(resolve(__dirname, "../../tests/fixtures/submit/observation_fingerprint_vector.json"),
  "utf8")) as { observation: ObservationV1; structure_fingerprint: string; observation_fingerprint: string };

describe("observation fingerprint (6E-A verification, mirrors product.fill_observation)", () => {
  it("equals Python's structure and observation fingerprints byte for byte", async () => {
    expect(await structureFingerprint(vector.observation)).toBe(vector.structure_fingerprint);
    expect(await observationFingerprint(vector.observation)).toBe(vector.observation_fingerprint);
  });

  it("changes when an application value state changes", async () => {
    const changed = structuredClone(vector.observation);
    changed.elements[1].value_state = { state: "NONBLANK", current_value_hash: "sha256:" + "8".repeat(64) } as never;
    expect(await observationFingerprint(changed)).not.toBe(vector.observation_fingerprint);
  });
});
