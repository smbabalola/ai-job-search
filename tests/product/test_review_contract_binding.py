# tests/product/test_review_contract_binding.py
from __future__ import annotations

import dataclasses

from hypothesis import given, strategies as st

from product.review_contract import (
    Claim, EvidenceRef, PlannedField, ProvenanceLabel, Reviewable, ReviewDocument, ReviewWarning, WarningLevel,
    approval_binding, binding_hash, claim_provenance_hash, component_hashes, delta_only, evidence_basis_hash,
    invalidation_reasons,
)


def claim(evidence_value="Python"):
    ev = EvidenceRef("clm_1", evidence_basis_hash({"id": "clm_1", "value": evidence_value}))
    return Claim("cv-1", "sha256:unit", ProvenanceLabel.PROFILE_EVIDENCE, (ev,))


def reviewable(**kw):
    docs = (ReviewDocument("cv", "doc_cv", "a" * 64, 100, "ai_generated", (claim(),)),
            ReviewDocument("cover_letter", "doc_cl", "b" * 64, 90, "user_uploaded", ()))
    fields = (PlannedField("subject:notice_period", "notice_period", True, "Notice period?", "ANSWER",
                           "APPROVED_ANSWER", "ans_1", "sha256:v", ("whitespace_normalize",), "1 month",
                           ProvenanceLabel.USER_SUPPLIED),
              PlannedField("subject:relocate", "relocate", False, "Relocate?", "OMIT", None, None, None, (), None,
                           None))
    warnings = (ReviewWarning("user_managed:cover_letter", WarningLevel.ATTENTION, "not verified", True),
                ReviewWarning("fit_score", WarningLevel.INFO, "fit 82", False))
    values = dict(account_id="acct", application_workspace_id="ws", job_identity_key="source:x:1",
                  identity_strength="SOURCE_RECORD", job_posting_content_id="job_1",
                  target_url="https://example.test/apply", target_provenance="discovery_verified",
                  pack_artifact_id="art_1", pack_content_hash="sha256:p", pack_schema_version="application-pack.v2",
                  documents=docs, fields=fields, warnings=warnings)
    values.update(kw)
    return Reviewable(**values)


def test_info_warnings_are_outside_the_binding_but_attention_and_blocking_are_in():
    base = approval_binding(reviewable())
    assert [w["warning_key"] for w in base["review_warnings"]] == ["user_managed:cover_letter"]
    more_info = reviewable(warnings=reviewable().warnings + (ReviewWarning("x", WarningLevel.INFO, "i", False),))
    assert binding_hash(approval_binding(more_info)) == binding_hash(base)
    blocking = ReviewWarning("answer_expired:k", WarningLevel.BLOCKING, "expired", False)
    assert binding_hash(approval_binding(reviewable(warnings=reviewable().warnings + (blocking,)))) != binding_hash(base)


def test_acknowledging_changes_the_binding():
    unacked = reviewable(warnings=(ReviewWarning("user_managed:cover_letter", WarningLevel.ATTENTION, "n", False),))
    assert binding_hash(approval_binding(unacked)) != binding_hash(approval_binding(reviewable()))


def test_evidence_content_change_with_same_ref_and_bytes_changes_the_digest():
    changed = ReviewDocument("cv", "doc_cv", "a" * 64, 100, "ai_generated", (claim("Rust"),))
    r2 = reviewable(documents=(changed,) + reviewable().documents[1:])
    assert claim_provenance_hash((claim("Rust"),)) != claim_provenance_hash((claim(),))
    assert invalidation_reasons(approval_binding(reviewable()), approval_binding(r2)) == ["document:cv"]


def test_user_managed_documents_carry_no_claim_digest():
    doc = next(d for d in approval_binding(reviewable())["documents"] if d["kind"] == "cover_letter")
    assert doc["claim_provenance_hash"] is None


def test_delta_only_exactly_when_non_delta_components_are_equal():
    base = approval_binding(reviewable())
    extra = PlannedField("subject:salary", "salary", True, "Salary?", "ANSWER", "APPROVED_ANSWER", "ans_2",
                         "sha256:s", (), "50k", ProvenanceLabel.USER_SUPPLIED)
    with_delta = approval_binding(reviewable(fields=reviewable().fields + (extra,)))
    assert delta_only(base, with_delta, {"subject:salary"}) == (True, ())
    also_target = approval_binding(reviewable(fields=reviewable().fields + (extra,), target_url="https://other.test"))
    assert delta_only(base, also_target, {"subject:salary"}) == (False, ("apply_target",))


@given(st.sampled_from(["job_identity_key", "job_posting_content_id", "target_url", "target_provenance",
                        "pack_artifact_id", "pack_content_hash"]), st.text(min_size=1, max_size=8))
def test_every_bound_scalar_changes_the_hash(field, suffix):
    base = reviewable()
    changed = dataclasses.replace(base, **{field: getattr(base, field) + suffix})
    assert binding_hash(approval_binding(changed)) != binding_hash(approval_binding(base))


def test_a_removed_delta_field_is_never_delta_only():
    base = approval_binding(reviewable())
    fewer = approval_binding(reviewable(fields=reviewable().fields[:1]))  # subject:relocate disappeared
    assert delta_only(base, fewer, {"subject:relocate"}) == (False, ("field:subject:relocate",))


def test_display_only_field_metadata_is_not_bound():
    base = reviewable()
    shown = reviewable(fields=tuple(dataclasses.replace(f, reach="ACCOUNT", freshness="valid until x")
                                    for f in base.fields))
    assert binding_hash(approval_binding(shown)) == binding_hash(approval_binding(base))


def test_effective_delta_key():
    from product.review_contract import effective_delta_key
    assert effective_delta_key({"id": "dlt_1", "answer_key": None}) == "delta:dlt_1"
    assert effective_delta_key({"id": "dlt_1", "answer_key": "subject:salary"}) == "subject:salary"
    assert effective_delta_key({"id": "dlt_1", "answer_key": None, "subject": "salary"}) == "subject:salary"


def test_warning_key_changes_with_its_material():
    from product.review_contract import warning_key
    a = warning_key("user_managed", "cover_letter", {"document_version_id": "doc_cl", "sha256": "b" * 64})
    b = warning_key("user_managed", "cover_letter", {"document_version_id": "doc_cl2", "sha256": "c" * 64})
    assert a.startswith("user_managed:cover_letter:sha256:") and len(a.split(":", 2)[2]) == len("sha256:") + 64 and a != b
    assert a == warning_key("user_managed", "cover_letter", {"sha256": "b" * 64, "document_version_id": "doc_cl"})


def test_component_names():
    assert set(component_hashes(approval_binding(reviewable()))) == {
        "job", "apply_target", "pack", "document:cv", "document:cover_letter", "field:subject:notice_period",
        "field:subject:relocate", "review_warnings"}


def _bound(**kw):
    return approval_binding(reviewable(**kw))


def test_delta_resolved_per_kind():
    from product.review_contract import delta_resolved
    b = _bound()
    field = {"id": "d1", "kind": "NEW_QUESTION", "answer_key": "subject:notice_period", "required": 1, "observed": {}}
    assert delta_resolved(field, b, document_media_types={})
    omitted_optional = {**field, "answer_key": "subject:relocate", "required": 0}
    assert delta_resolved(omitted_optional, b, document_media_types={})
    missing = {**field, "answer_key": "subject:salary"}
    assert not delta_resolved(missing, b, document_media_types={})
    failing = {**field, "kind": "TRANSFORM_FAILURE", "observed": {"value_hash": "sha256:v"}}
    assert not delta_resolved(failing, b, document_media_types={})
    assert delta_resolved({**failing, "observed": {"value_hash": "sha256:other"}}, b, document_media_types={})
    target = {"id": "t", "kind": "TARGET_CHANGE", "answer_key": None, "required": 1,
              "observed": {"canonical_url": "https://example.test/apply"}}
    assert delta_resolved(target, b, document_media_types={})
    assert not delta_resolved({**target, "observed": {"canonical_url": "https://elsewhere.test"}}, b,
                              document_media_types={})
    conversion = {"id": "c", "kind": "DOCUMENT_CONVERSION", "answer_key": None, "required": 1,
                  "observed": {"kind": "cv", "required_media_type": "application/pdf"}}
    assert not delta_resolved(conversion, b, document_media_types={"cv": "application/docx"})
    assert delta_resolved(conversion, b, document_media_types={"cv": "application/pdf"})
    upload = {"id": "u", "kind": "NEW_UPLOAD", "answer_key": None, "required": 1, "observed": {"kind": "portfolio"}}
    assert not delta_resolved(upload, b, document_media_types={})
