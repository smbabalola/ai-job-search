import { describe, expect, it } from "vitest";
import { buildCandidateSnapshotFromProjection } from "../src/background/snapshot-projection";

describe("buildCandidateSnapshotFromProjection", () => {
  it("maps requested and returned field types into the CandidateSnapshot shape", () => {
    const snapshot = buildCandidateSnapshotFromProjection({
      name: { value: "Ada Lovelace", profile_evidence_ids: ["c1"] },
      email: { value: "ada@example.com", profile_evidence_ids: ["c2"] },
    });

    expect(snapshot.identity.name).toEqual({ value: "Ada Lovelace", profile_evidence_ids: ["c1"] });
    expect(snapshot.contact.email).toEqual({ value: "ada@example.com", profile_evidence_ids: ["c2"] });
    expect(snapshot.contact.phone).toBeNull();
  });

  it("never fabricates a value for a field type the projection did not return", () => {
    const snapshot = buildCandidateSnapshotFromProjection({});
    expect(snapshot.identity.name).toBeUndefined();
    expect(snapshot.contact.email).toBeNull();
    expect(snapshot.contact.phone).toBeNull();
    expect(snapshot.contact.linkedin).toBeNull();
    expect(snapshot.contact.github).toBeNull();
    expect(snapshot.contact.location).toBeNull();
    expect(snapshot.employment).toEqual([]);
  });

  it("maps employment[0].employer into the employment array's employer field only", () => {
    const snapshot = buildCandidateSnapshotFromProjection({
      "employment[0].employer": { value: "Acme Corp", profile_evidence_ids: ["c3"] },
    });
    expect(snapshot.employment).toHaveLength(1);
    expect(snapshot.employment[0].employer).toEqual({ value: "Acme Corp", profile_evidence_ids: ["c3"] });
    expect(snapshot.employment[0].role).toBeNull();
  });

  it("does not release years_of_experience or any other unmapped/unsupported type as candidate data", () => {
    // years_of_experience/legal_declaration/unknown never appear in a real
    // server response (project_session_snapshot's closed mapping excludes
    // them), but even if one were present here, this mapper has no slot
    // for it — it silently has nowhere to go, never smuggled into an
    // existing field.
    const snapshot = buildCandidateSnapshotFromProjection({
      years_of_experience: { value: "5", profile_evidence_ids: [] },
    } as never);
    expect(snapshot.contact).not.toHaveProperty("years_of_experience");
    expect(JSON.stringify(snapshot)).not.toContain("years_of_experience");
  });
});
