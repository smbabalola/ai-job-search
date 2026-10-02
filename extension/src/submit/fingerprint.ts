// Python's product.fill_observation.observation_fingerprint: the structure
// fingerprint plus every APPLICATION element's value state. The
// SubmitController proves the PRE_SUBMIT page with it (spec §8.3);
// tests/fixtures/submit/observation_fingerprint_vector.json pins both sides.
import { canonicalHash, type Canonical } from "../fill/canonical";
import type { ObservationV1 } from "../fill/observation-types";
import { structureFingerprint } from "../fill/observer";

export async function observationFingerprint(obs: ObservationV1): Promise<string> {
  const valueStates: Record<string, Canonical> = {};
  for (const e of obs.elements) {
    if (e.classification === "APPLICATION") valueStates[e.page_field_key] = e.value_state as unknown as Canonical;
  }
  return canonicalHash("fill-observation", "v1", {
    structure_fingerprint: await structureFingerprint(obs), value_states: valueStates,
  });
}
