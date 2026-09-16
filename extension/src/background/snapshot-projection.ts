import type { CandidateSnapshot, ProvenancedValue } from "../adapters/types";

// The server's projection response (POST .../sessions/{id}/snapshot) is
// keyed directly by normalized_field_type -> ProvenancedValue (see
// project_session_snapshot / _NORMALIZED_FIELD_TYPE_TO_CANDIDATE_PATH in
// webapp/services/handoff.py), NOT already shaped as a CandidateSnapshot.
// This module reconstructs the minimal CandidateSnapshot the existing
// adapters/content-script expect, containing ONLY the keys the server
// actually returned — a field type never requested, or requested but
// absent from the candidate's own data, is simply missing here, never
// filled with a fabricated/empty value.
export type SessionSnapshotProjection = Record<string, ProvenancedValue>;

const CONTACT_FIELD_TYPES = ["email", "phone", "linkedin", "github", "location"] as const;

export function buildCandidateSnapshotFromProjection(
  projection: Record<string, unknown>,
): CandidateSnapshot {
  const typedProjection = projection as SessionSnapshotProjection;
  const contact: Record<string, ProvenancedValue | null> = {};
  for (const fieldType of CONTACT_FIELD_TYPES) {
    contact[fieldType] = typedProjection[fieldType] ?? null;
  }

  const employerValue = typedProjection["employment[0].employer"];
  const employment: CandidateSnapshot["employment"] = employerValue
    ? [{
        record_id: "session_projection_0",
        role: null, date_range: null, location: null, details: [],
        employer: employerValue,
      }]
    : [];

  return {
    // identity.name is typed as required, but every consumer already
    // guards with optional chaining (e.g. snapshot.identity.name?.value)
    // — never present a fabricated name when the server did not return
    // one (it was not requested, or the candidate has none on file).
    identity: { name: typedProjection["name"] as ProvenancedValue },
    contact,
    employment,
  };
}
