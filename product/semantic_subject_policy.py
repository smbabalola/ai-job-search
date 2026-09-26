"""Per-subject policy data for the autonomy contract (6B spec §6). Tuned by
editing semantic_subject_policy.v1.json, never the engine."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from product.autonomy_contract import REACH_ORDER, Reach, canonical_hash
from product.semantic_subject_registry import SEMANTIC_SUBJECTS

SUBJECT_POLICY_SCHEMA = "semantic-subject-policy"
SUBJECT_POLICY_SCHEMA_VERSION = "semantic-subject-policy.v1"
DEFAULT_PATH = Path(__file__).with_name("semantic_subject_policy.v1.json")
SENSITIVE_CLASSES = {"legal_attestation", "demographic", "criminal_record", "health"}
_ENTRY_KEYS = {"answer_kind", "max_reach", "default_reach", "context_keys", "freshness_days", "submit_eligible",
               "sensitive", "requires_current_profile_basis"}


class SubjectPolicyError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_subject_policy(doc: Any) -> None:
    errors: list[str] = []
    if not isinstance(doc, dict) or doc.get("schema_version") != SUBJECT_POLICY_SCHEMA_VERSION:
        raise SubjectPolicyError(["schema_version: must be semantic-subject-policy.v1"])
    subjects = doc.get("subjects")
    if not isinstance(subjects, dict):
        raise SubjectPolicyError(["subjects: must be an object"])
    for key in sorted(set(SEMANTIC_SUBJECTS) - set(subjects)):
        errors.append(f"subjects: missing registry subject {key!r}")
    for key in sorted(set(subjects) - set(SEMANTIC_SUBJECTS)):
        errors.append(f"subjects: unknown subject {key!r} (add it to the registry first)")
    for key, entry in subjects.items():
        path = f"subjects.{key}"
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            errors.append(f"{path}: keys must be exactly {sorted(_ENTRY_KEYS)}")
            continue
        if entry["answer_kind"] not in ("STRUCTURED", "FREE_TEXT"):
            errors.append(f"{path}.answer_kind: must be STRUCTURED or FREE_TEXT")
        reaches = {r.value for r in Reach}
        if entry["max_reach"] not in reaches or entry["default_reach"] not in reaches:
            errors.append(f"{path}: max_reach/default_reach must be one of {sorted(reaches)}")
        elif REACH_ORDER[Reach(entry["default_reach"])] > REACH_ORDER[Reach(entry["max_reach"])]:
            errors.append(f"{path}.default_reach: must not exceed max_reach")
        if not isinstance(entry["context_keys"], list) or not all(isinstance(k, str) for k in entry["context_keys"]):
            errors.append(f"{path}.context_keys: must be a list of strings")
        days = entry["freshness_days"]
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 1):
            errors.append(f"{path}.freshness_days: must be null or a positive integer")
        if not isinstance(entry["submit_eligible"], bool):
            errors.append(f"{path}.submit_eligible: must be a boolean")
        if not isinstance(entry["requires_current_profile_basis"], bool):
            errors.append(f"{path}.requires_current_profile_basis: must be a boolean")
        if entry["sensitive"] is not None:
            if entry["sensitive"] not in SENSITIVE_CLASSES:
                errors.append(f"{path}.sensitive: must be null or one of {sorted(SENSITIVE_CLASSES)}")
            if entry["submit_eligible"] is not False:
                errors.append(f"{path}: sensitive subjects must have submit_eligible=false in v1")
    if errors:
        raise SubjectPolicyError(errors)


def load_subject_policy(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_subject_policy(doc)
    return doc


def subject_policy_hash(doc: Mapping[str, Any]) -> str:
    return canonical_hash(SUBJECT_POLICY_SCHEMA, SUBJECT_POLICY_SCHEMA_VERSION, doc)


def subject_entry(doc: Mapping[str, Any], subject: str | None) -> dict[str, Any] | None:
    if subject is None:
        return None
    return doc["subjects"].get(subject)
