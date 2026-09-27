"""Bundle 6D-A: assemble the reviewable application and its review state
(spec §5, §6, §6.1, §7.1-7.2, §8.3). Read only.

Everything that affects what would be sent, or what is presented as its
basis, is assembled here from current state. Identity, employer key and
apply target come from the same functions the 6B authorization context
uses, so an approval binds exactly what authorization will later check."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from product.application_pack_v2_contract import validate_application_pack_v2
from product.autonomy_contract import canonical_hash, parse_utc
from product.review_contract import (
    Claim, EvidenceRef, ProvenanceLabel, Reviewable, ReviewDocument, ReviewSnapshot, ReviewState, ReviewWarning,
    WarningLevel, claim_content_hash, claim_provenance_hash, derive_review_state, effective_delta_key,
    evidence_basis_hash, warning_key,
)
from webapp.config import Settings
from webapp.persistence import review_approval as ra
from webapp.persistence.application_documents import get_document_version, get_selection
from webapp.persistence.application_identity import get_search_workspace_for_application
from webapp.persistence.artifacts import get_artifact, get_current_artifact
from webapp.persistence.autonomy_ledger import workspace_identity
from webapp.persistence.workspaces import get_profile_workspace_id, get_workspace
from webapp.services.autonomy_context import apply_target_state, employer_identity
from webapp.services.review_fields import planned_fields

KINDS = ("cv", "cover_letter")


class ReviewRefused(Exception):
    """A review action refused for a stated reason (the API maps it to 409)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _float_safe_payload(value: Any) -> Any:
    from decimal import Decimal
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _float_safe_payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_float_safe_payload(v) for v in value]
    return value
_UNITS = {"cv": "cv_content", "cover_letter": "cover_letter_content"}
V2 = "application-pack.v2"


def _profile_payload(conn, account_id: str) -> dict[str, Any]:
    profile_ws = get_profile_workspace_id(conn, account_id)
    artifact = get_current_artifact(conn, profile_ws, "profile_snapshot") if profile_ws else None
    return artifact["payload"] if artifact else {}


def _claims_for(unit_source: list[Mapping[str, Any]], profile: Mapping[str, Any]) -> tuple[Claim, ...]:
    conflicted = {c.get("concept_id") for c in profile.get("conflicts") or []}
    records = {c["id"]: c for c in profile.get("claims") or []}
    valid = {cid for cid, c in records.items() if not c.get("placeholder") and c.get("concept_id") not in conflicted}
    out = []
    for unit in unit_source:
        atoms = unit.get("atoms") or []
        refs = sorted(set(unit.get("profile_evidence_ids") or [])
                      | {r for a in atoms for r in (a.get("profile_evidence_ids") or [])})
        evidence = tuple(EvidenceRef(r, evidence_basis_hash(records[r]) if r in records
                                     else evidence_basis_hash({"missing": r})) for r in refs)
        if any(r not in valid for r in refs):
            label = ProvenanceLabel.UNKNOWN
        elif all(a.get("atom_kind") == "candidate_fact" for a in atoms):
            label = ProvenanceLabel.PROFILE_EVIDENCE
        else:
            label = ProvenanceLabel.AI_DERIVED
        out.append(Claim(unit["unit_id"], claim_content_hash(unit), label, evidence))
    return tuple(out)


def _document(conn, account_id: str, kind: str, version_id: str,
              profile: Mapping[str, Any]) -> tuple[ReviewDocument | None, dict[str, Any] | None]:
    row = get_document_version(conn, version_id, account_id=account_id)
    if row is None:
        return None, None
    claims: tuple[Claim, ...] = ()
    if row["origin"] == "ai_generated" and row.get("source_generation_artifact_id"):
        generation = get_artifact(conn, row["source_generation_artifact_id"])
        basis = (generation or {}).get("payload", {}).get("reviewed_application_pack") or {}
        claims = _claims_for(list(basis.get(_UNITS[kind]) or []), profile)
    return ReviewDocument(kind, row["id"], row["sha256"], row["byte_length"], row["origin"], claims), row


def _document_warnings(doc: ReviewDocument) -> list[ReviewWarning]:
    out = []
    if doc.origin == "user_uploaded":
        out.append(ReviewWarning(warning_key("user_managed", doc.kind, {"document_version_id": doc.document_version_id,
                                                                         "sha256": doc.sha256}),
                                 WarningLevel.ATTENTION, f"Your {doc.kind.replace('_', ' ')} is not content-verified"))
    for c in doc.claims:
        if c.label is ProvenanceLabel.UNKNOWN:
            out.append(ReviewWarning(warning_key("unknown_source", c.claim_id, {
                "document_version_id": doc.document_version_id, "content_hash": c.content_hash,
                "evidence": [(e.ref, e.basis_hash) for e in c.evidence]}),
                WarningLevel.BLOCKING, f"{c.claim_id}: its profile evidence is missing"))
    if any(c.label is ProvenanceLabel.AI_DERIVED for c in doc.claims):
        out.append(ReviewWarning(warning_key("ai_derived_claims", doc.kind, {
            "document_version_id": doc.document_version_id, "claim_provenance_hash": claim_provenance_hash(doc.claims)}),
            WarningLevel.ATTENTION, f"Your {doc.kind.replace('_', ' ')} contains AI-derived claims"))
    return out


def _assemble(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
              now: datetime) -> tuple[Reviewable | None, bool, dict[str, Any]]:
    """(reviewable, exact_pack, workspace)."""
    ws = application_workspace_id
    workspace = get_workspace(conn, ws, account_id=account_id)
    if workspace is None or workspace.get("kind") != "job":
        raise LookupError(ws)
    pack = get_current_artifact(conn, ws, "application_pack")
    selections = {k: get_selection(conn, ws, k, account_id=account_id) for k in KINDS}
    if pack is None and not any(selections.values()):
        return None, False, workspace
    profile = _profile_payload(conn, account_id)
    warnings: list[ReviewWarning] = []

    payload = pack["payload"] if pack else {}
    is_v2 = False
    if payload.get("schema_version") == V2:
        try:
            validate_application_pack_v2(payload)
            is_v2 = True
        except ValueError:  # a malformed v2 artifact is never trusted
            is_v2 = False
    final = payload.get("final_documents") or {} if is_v2 else {}
    stored = payload.get("selection_revisions") or {} if is_v2 else {}
    exact = bool(is_v2 and stored and all(
        selections[k] and final.get(k, {}).get("document_version_id") == selections[k]["document_version_id"]
        and stored.get(k) == selections[k]["revision"] for k in KINDS))
    documents: list[ReviewDocument] = []
    for kind in KINDS:
        version_id = final[kind]["document_version_id"] if exact else (selections[kind] or {}).get("document_version_id")
        if version_id:
            doc, _ = _document(conn, account_id, kind, version_id, profile)
            if doc is not None:
                documents.append(doc)
                warnings.extend(_document_warnings(doc))
    if not settings.cv_quality_v2_enabled or (pack is not None and not is_v2 and not any(selections.values())):
        exact = False
        warnings.append(ReviewWarning(warning_key("exact_files_required", "documents", {
            "pack_schema_version": payload.get("schema_version"), "v2_enabled": bool(settings.cv_quality_v2_enabled)}),
            WarningLevel.BLOCKING, "Exact document files are required before approval"))
    elif not exact:
        warnings.append(ReviewWarning(warning_key("save_document_changes", "documents", {
            "selected": {k: (selections[k] or {}).get("document_version_id") for k in KINDS},
            "pack_artifact_id": pack["id"] if pack else None}),
            WarningLevel.BLOCKING, "Save your document changes to review the exact files"))

    identity_key, identity_strength, conflict = workspace_identity(conn, ws)
    if conflict:
        warnings.append(ReviewWarning(warning_key("identity_conflict", "job", {"identity_key": identity_key}),
                                      WarningLevel.BLOCKING, "This job's identity conflicts with another application"))
    target_url, provenance = apply_target_state(conn, workspace_id=ws, account_id=account_id, identity_key=identity_key)
    if target_url is None:
        warnings.append(ReviewWarning(warning_key("no_apply_target", "apply_target", {}), WarningLevel.BLOCKING,
                                      "There is no known apply target"))
    elif provenance is not None and provenance.value == "user_supplied":
        warnings.append(ReviewWarning(warning_key("target_user_supplied", "apply_target", {
            "canonical_url": target_url, "provenance": provenance.value}),
            WarningLevel.ATTENTION, "You supplied this apply target; it is not a verified submission destination"))

    posting_artifact = get_current_artifact(conn, ws, "job_posting_snapshot")
    posting = posting_artifact["payload"] if posting_artifact else {}
    employer_key, employer_strength = employer_identity(posting, workspace)
    fields, field_warnings = planned_fields(
        conn, account_id=account_id, application_workspace_id=ws, profile_payload=profile,
        employer_key=employer_key, employer_key_strength=employer_strength,
        search_workspace_id=get_search_workspace_for_application(conn, ws), now=now)
    warnings.extend(field_warnings)
    from webapp.services.review_documents import newer_draft_warnings  # after review_application loads
    warnings.extend(newer_draft_warnings(conn, account_id=account_id, application_workspace_id=ws))
    fit = get_current_artifact(conn, ws, "job_fit_result")
    score = ((fit or {}).get("payload") or {}).get("overall_score")
    if score is not None:
        warnings.append(ReviewWarning("fit_score", WarningLevel.INFO, f"Fit score {score}"))

    acknowledged = ra.acknowledged_warning_keys(conn, ws)
    warnings = [ReviewWarning(w.key, w.level, w.message, w.key in acknowledged) for w in warnings]
    reviewable = Reviewable(
        account_id=account_id, application_workspace_id=ws, job_identity_key=identity_key,
        identity_strength=identity_strength.value, job_posting_content_id=(posting_artifact or {}).get("content_id"),
        target_url=target_url, target_provenance=provenance.value if provenance else None,
        pack_artifact_id=pack["id"] if pack else None,
        pack_content_hash=canonical_hash("application-pack-content", "v1", _float_safe_payload(payload)) if pack else None,
        pack_schema_version=payload.get("schema_version") if pack else None,
        documents=tuple(documents), fields=fields, warnings=tuple(sorted(warnings, key=lambda w: w.key)))
    return reviewable, exact, workspace


def build_reviewable(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                     now: datetime) -> Reviewable | None:
    return _assemble(conn, settings=settings, account_id=account_id, application_workspace_id=application_workspace_id,
                     now=now)[0]


def review_snapshot(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                    now: datetime) -> ReviewSnapshot:
    ws = application_workspace_id
    reviewable, exact, workspace = _assemble(conn, settings=settings, account_id=account_id,
                                             application_workspace_id=ws, now=now)
    latest = ra.latest_approval(conn, ws)
    latest_view = None if latest is None else {
        "binding_hash": latest["binding_hash"], "created_at": parse_utc(latest["created_at"]),
        "revoked": ra.approval_revoked(conn, latest)}
    return ReviewSnapshot(
        reviewable=reviewable, workflow_status=workspace.get("workflow_status"), latest_approval=latest_view,
        open_delta_keys=tuple(sorted(effective_delta_key(d) for d in ra.open_deltas(conn, ws))), now=now,
        ttl=timedelta(days=settings.review_approval_ttl_days), exact_pack=exact)


def review_state(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                 now: datetime) -> ReviewState:
    return derive_review_state(review_snapshot(conn, settings=settings, account_id=account_id,
                                               application_workspace_id=application_workspace_id, now=now))
