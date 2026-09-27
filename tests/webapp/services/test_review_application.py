from __future__ import annotations

from datetime import timedelta

from product.autonomy_contract import Capability
from product.review_contract import ProvenanceLabel, approval_binding, binding_hash
from webapp.persistence import review_approval as ra
from webapp.persistence.artifacts import get_current_artifact, save_artifact
from webapp.persistence.autonomy_ledger import workspace_identity
from tests.webapp.services.review_fixtures import NOW, V2_ACCOUNT, answer, blocker, v2_chain  # noqa: F401


def _keys(state):
    return {b.split(":", 1)[0] for b in state.blocking}


def _approve_current(world):
    state = world.state()
    return ra.insert_approval(world.conn, account_id=V2_ACCOUNT, application_workspace_id=world.ws,
                              binding=state.binding, binding_hash=state.binding_hash, supersedes_id=None,
                              batch_id=None, resolved_delta_ids=[], actor="u", now=NOW)


def _replace_profile(world, mutate):
    from webapp.persistence.workspaces import get_profile_workspace_id
    profile_ws = get_profile_workspace_id(world.conn, V2_ACCOUNT)
    payload = dict(get_current_artifact(world.conn, profile_ws, "profile_snapshot")["payload"])
    payload["claims"] = mutate([dict(c) for c in payload["claims"]])
    save_artifact(world.conn, workspace_id=profile_ws, artifact_type="profile_snapshot", payload=payload)


def test_v2_chain_builds_the_full_reviewable(v2_chain):
    r = v2_chain.reviewable()
    assert {d.kind for d in r.documents} == {"cv", "cover_letter"}
    assert all(d.origin == "ai_generated" and d.claims for d in r.documents)
    assert {c.label for d in r.documents for c in d.claims} == {ProvenanceLabel.PROFILE_EVIDENCE}
    assert r.pack_schema_version == "application-pack.v2" and r.target_url and r.target_provenance == "user_supplied"
    state = v2_chain.state()
    assert state.binding_hash is not None and state.state == "READY_FOR_REVIEW"


def test_selection_change_without_save_blocks_and_exposes_no_binding_hash(v2_chain):
    from webapp.services.application_documents import select_application_document, upload_application_document
    from tests.webapp.services.review_fixtures import docx_bytes
    uploaded = upload_application_document(v2_chain.conn, v2_chain.ws, kind="cv", filename="mine.docx",
                                           content=docx_bytes("my own cv"), documents_root=v2_chain.settings.documents_root,
                                           account_id=V2_ACCOUNT)
    select_application_document(v2_chain.conn, v2_chain.ws, kind="cv", document_version_id=uploaded["id"],
                                expected_revision=v2_chain.selection("cv")["revision"], account_id=V2_ACCOUNT)
    state = v2_chain.state()
    assert "save_document_changes" in _keys(state) and state.binding_hash is None and state.provisional_hash


def test_v1_or_v2_disabled_blocks_with_exact_files_required(v2_chain):
    state = v2_chain.state(cv_quality_v2_enabled=False)
    assert "exact_files_required" in _keys(state) and state.binding_hash is None


def test_removed_evidence_blocks_and_invalidates(v2_chain):
    _approve_current(v2_chain)
    assert v2_chain.state().binding_matches
    _replace_profile(v2_chain, lambda claims: [])  # the cited claim is gone
    state = v2_chain.state()
    assert "unknown_source" in _keys(state) and not state.approval_effective and "binding_changed" in state.reasons


def test_provenance_change_with_identical_bytes_invalidates(v2_chain):
    before = v2_chain.state()
    _replace_profile(v2_chain, lambda claims: [{**c, "value": f"{c.get('value')} (edited)"} for c in claims])
    after = v2_chain.state()
    assert before.binding["documents"] and [d["sha256"] for d in after.binding["documents"]] == \
        [d["sha256"] for d in before.binding["documents"]]
    assert after.binding_hash != before.binding_hash


def test_new_attention_warning_invalidates_immediately(v2_chain):
    v2_chain.set_target(provenance="discovery_verified")
    before = v2_chain.state()
    v2_chain.set_target(provenance="user_supplied")  # a new ATTENTION warning appears
    after = v2_chain.state()
    assert after.binding_hash != before.binding_hash


def test_answer_expiry_invalidates_immediately(v2_chain):
    blocker(v2_chain.conn, v2_chain.ws, "employment.notice_period")
    answer(v2_chain.conn, "employment.notice_period", "1 month")
    fresh = v2_chain.state(now=NOW)
    expired = v2_chain.state(now=NOW + timedelta(days=61))
    assert fresh.binding_hash != expired.binding_hash and "answer_expired" in _keys(expired)


def test_policy_capability_budget_pause_and_kill_switch_do_not_change_the_hash(v2_chain):
    from webapp.services.autonomy_controls import engage_kill_switch, pause, set_capability
    before = v2_chain.state().binding_hash
    set_capability(v2_chain.conn, account_id=V2_ACCOUNT, scope_type="ACCOUNT_MAX", scope_id=V2_ACCOUNT,
                   capability=Capability.PREPARE, actor="u", now=NOW)
    pause(v2_chain.conn, account_id=V2_ACCOUNT, scope_type="APPLICATION", scope_id=v2_chain.ws, actor="u",
          reason="r", now=NOW)
    engage_kill_switch(v2_chain.conn, account_id=V2_ACCOUNT, actor="u", reason="stop", now=NOW)
    assert v2_chain.state().binding_hash == before


def test_acknowledgement_does_not_carry_to_changed_material(v2_chain):
    v2_chain.set_target(url="https://jobs.example.test/acme/1")
    key = next(w.key for w in v2_chain.reviewable().warnings if w.key.startswith("target_user_supplied"))
    ra.record_event(v2_chain.conn, account_id=V2_ACCOUNT, application_workspace_id=v2_chain.ws,
                    event="WARNING_ACKNOWLEDGED", binding_hash=None, detail={"warning_key": key}, actor="u", now=NOW)
    assert next(w for w in v2_chain.reviewable().warnings if w.key == key).acknowledged
    v2_chain.set_target(url="https://jobs.example.test/acme/2")
    changed = next(w for w in v2_chain.reviewable().warnings if w.key.startswith("target_user_supplied"))
    assert changed.key != key and not changed.acknowledged


def test_identity_matches_workspace_identity_used_by_6b(v2_chain):
    key, strength, _ = workspace_identity(v2_chain.conn, v2_chain.ws)
    r = v2_chain.reviewable()
    assert (r.job_identity_key, r.identity_strength) == (key, strength.value)
