"""Fill-manifest schema (6B spec §8.3): exactly what Job Pipeline authorizes
an executor to place into an employer form. 6B defines schema, validation and
hashing; 6D builds manifests from live pages."""
from __future__ import annotations

from typing import Any, Mapping

from product.autonomy_contract import canonical_hash
from product.representation_transforms import TRANSFORM_IDS
from product.semantic_subject_registry import SEMANTIC_SUBJECTS

FILL_MANIFEST_SCHEMA = "fill-manifest"
FILL_MANIFEST_SCHEMA_VERSION = "fill-manifest.v1"
SOURCE_KINDS = {"EVIDENCE", "APPROVED_ANSWER", "PACK_DOCUMENT"}
_TOP = {"schema_version", "application_workspace_id", "adapter_id", "adapter_version", "pages"}
_ENTRY = {"page_field_key", "normalized_field_type", "subject", "source", "transform_id", "value_hash", "required"}


class FillManifestError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def value_hash(value: Any) -> str:
    return canonical_hash("fill-value", "v1", value)


def validate_fill_manifest(doc: Any) -> None:
    errors: list[str] = []
    if not isinstance(doc, dict) or set(doc) != _TOP:
        raise FillManifestError([f"manifest keys must be exactly {sorted(_TOP)}"])
    if doc["schema_version"] != FILL_MANIFEST_SCHEMA_VERSION:
        errors.append("schema_version: must be fill-manifest.v1")
    page_keys: set[str] = set()
    for p, page in enumerate(doc["pages"]):
        path = f"pages[{p}]"
        if not isinstance(page, dict) or set(page) != {"page_key", "entries"}:
            errors.append(f"{path}: keys must be page_key and entries")
            continue
        if page["page_key"] in page_keys:
            errors.append(f"{path}.page_key: duplicate page {page['page_key']!r}")
        page_keys.add(page["page_key"])
        field_keys: set[str] = set()
        for e, entry in enumerate(page["entries"]):
            epath = f"{path}.entries[{e}]"
            if not isinstance(entry, dict) or set(entry) != _ENTRY:
                errors.append(f"{epath}: keys must be exactly {sorted(_ENTRY)}")
                continue
            if entry["page_field_key"] in field_keys:
                errors.append(f"{epath}.page_field_key: duplicate field {entry['page_field_key']!r}")
            field_keys.add(entry["page_field_key"])
            if (entry["normalized_field_type"] is None) == (entry["subject"] is None):
                errors.append(f"{epath}: exactly one of normalized_field_type or subject is required")
            if entry["subject"] is not None and entry["subject"] not in SEMANTIC_SUBJECTS:
                errors.append(f"{epath}.subject: unknown subject {entry['subject']!r}")
            source = entry["source"]
            if not isinstance(source, dict) or set(source) != {"kind", "ref", "confirmation_id"}:
                errors.append(f"{epath}.source: keys must be kind, ref, confirmation_id")
            else:
                if source["kind"] not in SOURCE_KINDS:
                    errors.append(f"{epath}.source.kind: must be one of {sorted(SOURCE_KINDS)}")
                if source["kind"] == "APPROVED_ANSWER" and not source["confirmation_id"]:
                    errors.append(f"{epath}.source.confirmation_id: required for APPROVED_ANSWER")
            if entry["transform_id"] not in TRANSFORM_IDS:
                errors.append(f"{epath}.transform_id: unknown transform {entry['transform_id']!r}")
            if not isinstance(entry["value_hash"], str) or not entry["value_hash"].startswith("sha256:"):
                errors.append(f"{epath}.value_hash: must be a sha256 hash")
            if not isinstance(entry["required"], bool):
                errors.append(f"{epath}.required: must be a boolean")
    if errors:
        raise FillManifestError(errors)


def manifest_hash(doc: Mapping[str, Any]) -> str:
    validate_fill_manifest(doc)
    normalized = dict(doc)
    normalized["pages"] = [
        {"page_key": page["page_key"],
         "entries": sorted(page["entries"], key=lambda entry: entry["page_field_key"])}
        for page in doc["pages"]
    ]
    return canonical_hash(FILL_MANIFEST_SCHEMA, FILL_MANIFEST_SCHEMA_VERSION, normalized)
