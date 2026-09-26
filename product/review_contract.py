# product/review_contract.py
"""Bundle 6D-A pure review contract (spec §5, §6.1, §8.3, §9.1, §11.1).
No IO, no clock; webapp is never imported."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

from product.autonomy_contract import canonical_hash

BINDING_SCHEMA = "application-approval-binding.v1"
REVIEW_CONTRACT_VERSION = "review-contract.v1"


class WarningLevel(str, Enum):
    BLOCKING = "BLOCKING"
    ATTENTION = "ATTENTION"
    INFO = "INFO"


class ProvenanceLabel(str, Enum):
    PROFILE_EVIDENCE = "profile_evidence"
    AI_DERIVED = "ai_derived"
    USER_SUPPLIED = "user_supplied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReviewWarning:
    key: str
    level: WarningLevel
    message: str
    acknowledged: bool = False


@dataclass(frozen=True)
class EvidenceRef:
    ref: str
    basis_hash: str


@dataclass(frozen=True)
class Claim:
    claim_id: str
    content_hash: str
    label: ProvenanceLabel
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class ReviewDocument:
    kind: str
    document_version_id: str
    sha256: str
    byte_length: int
    origin: str
    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class PlannedField:
    answer_key: str
    subject: str | None
    required: bool
    question: str
    disposition: str | None  # "ANSWER" | "OMIT" | None (undecided)
    source_kind: str | None  # "EVIDENCE" | "APPROVED_ANSWER"
    source_ref: str | None
    value_hash: str | None
    permitted_transforms: tuple[str, ...]
    display_value: str | None
    provenance_label: ProvenanceLabel | None
    reach: str | None = None      # display only (spec §6); not bound
    freshness: str | None = None  # display only, e.g. "valid until 2026-10-26"; not bound


@dataclass(frozen=True)
class Reviewable:
    account_id: str
    application_workspace_id: str
    job_identity_key: str | None
    identity_strength: str | None
    job_posting_content_id: str | None
    target_url: str | None
    target_provenance: str | None
    pack_artifact_id: str | None
    pack_content_hash: str | None
    pack_schema_version: str | None
    documents: tuple[ReviewDocument, ...]
    fields: tuple[PlannedField, ...]
    warnings: tuple[ReviewWarning, ...]


def _safe(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def evidence_basis_hash(record: Mapping[str, Any]) -> str:
    """The immutable content of one cited evidence item as presented."""
    return canonical_hash("profile-evidence-basis", "v1", _safe(dict(record)))


def claim_content_hash(unit: Mapping[str, Any]) -> str:
    return canonical_hash("review-claim-content", "v1", _safe(dict(unit)))


def claim_provenance_hash(claims: Iterable[Claim]) -> str | None:
    items = sorted(({"claim_id": c.claim_id, "content_hash": c.content_hash, "label": c.label.value,
                     "evidence": sorted(({"ref": e.ref, "basis_hash": e.basis_hash} for e in c.evidence),
                                        key=lambda e: e["ref"])}
                    for c in claims), key=lambda c: c["claim_id"])
    return canonical_hash("claim-provenance", "v1", items) if items else None


def approval_binding(r: Reviewable) -> dict[str, Any]:
    return {
        "schema_version": BINDING_SCHEMA,
        "account_id": r.account_id, "application_workspace_id": r.application_workspace_id,
        "job": {"identity_key": r.job_identity_key, "identity_strength": r.identity_strength,
                "job_posting_content_id": r.job_posting_content_id},
        "apply_target": {"canonical_url": r.target_url, "provenance": r.target_provenance},
        "pack": {"artifact_id": r.pack_artifact_id, "content_hash": r.pack_content_hash,
                 "schema_version": r.pack_schema_version},
        "documents": sorted(({"kind": d.kind, "document_version_id": d.document_version_id, "sha256": d.sha256,
                              "byte_length": d.byte_length, "origin": d.origin,
                              "claim_provenance_hash": claim_provenance_hash(d.claims)
                              if d.origin == "ai_generated" else None}
                             for d in r.documents), key=lambda d: d["kind"]),
        "fields": sorted(({"answer_key": f.answer_key, "subject": f.subject, "required": f.required,
                           "disposition": f.disposition, "source_kind": f.source_kind, "source_ref": f.source_ref,
                           "value_hash": f.value_hash, "permitted_transforms": sorted(f.permitted_transforms)}
                          for f in r.fields), key=lambda f: f["answer_key"]),
        "review_warnings": sorted(({"warning_key": w.key, "level": w.level.value, "acknowledged": w.acknowledged}
                                   for w in r.warnings if w.level is not WarningLevel.INFO),
                                  key=lambda w: w["warning_key"]),
        "review_contract_version": REVIEW_CONTRACT_VERSION,
    }


def binding_hash(binding: Mapping[str, Any]) -> str:
    return canonical_hash("application-approval-binding", "v1", dict(binding))


def component_hashes(binding: Mapping[str, Any]) -> dict[str, str]:
    out = {name: canonical_hash("approval-component", "v1", binding[name])
           for name in ("job", "apply_target", "pack", "review_warnings")}
    for d in binding["documents"]:
        out[f"document:{d['kind']}"] = canonical_hash("approval-component", "v1", d)
    for f in binding["fields"]:
        out[f"field:{f['answer_key']}"] = canonical_hash("approval-component", "v1", f)
    return out


def invalidation_reasons(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    a, b = component_hashes(old), component_hashes(new)
    return sorted(name for name in set(a) | set(b) if a.get(name) != b.get(name))


def delta_only(previous: Mapping[str, Any], current: Mapping[str, Any],
               delta_keys: Iterable[str]) -> tuple[bool, tuple[str, ...]]:
    allowed = {f"field:{k}" for k in delta_keys}
    before, after = component_hashes(previous), component_hashes(current)
    removed = {name for name in before if name not in after}  # nothing of P may disappear, delta fields included
    other = tuple(c for c in invalidation_reasons(previous, current) if c not in allowed or c in removed)
    return not other, other


FIELD_DELTA_KINDS = frozenset({"NEW_QUESTION", "CHANGED_QUESTION", "DECLARATION", "TRANSFORM_FAILURE",
                               "OMIT_FIELD_REQUIRED"})
NON_FIELD_DELTA_KINDS = frozenset({"NEW_UPLOAD", "DOCUMENT_CONVERSION", "TARGET_CHANGE"})


def effective_delta_key(delta: Mapping[str, Any]) -> str:
    """The one canonical key of a delta (spec §11): its answer_key, or
    delta:<id> when it has none."""
    if delta.get("answer_key"):
        return delta["answer_key"]
    return f"subject:{delta['subject']}" if delta.get("subject") else f"delta:{delta['id']}"


def warning_key(warning_type: str, subject: str, material: Mapping[str, Any]) -> str:
    """type + subject + a fingerprint of the exact material that caused the
    warning (spec §6.1): changed material gives a new key, so an old
    acknowledgement never acknowledges it."""
    fp = canonical_hash("review-warning-material", "v1", _safe(dict(material)))  # full SHA-256
    return f"{warning_type}:{subject}:{fp}"
