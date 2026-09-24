"""Pure contract for the immutable reviewed CV Generation Quality v2 basis.

A ``cv-generation-basis.v1`` payload is the exact, reviewed input Task 4 will
eventually render: it pins the exact upstream artifact lineage, the exact
review decisions/authorization state consulted, and the exact
``cv-document-model.v2`` Task 3 produced from the authorized-only statement
projection. This module performs no database access, no artifact lookup, and
calls no Task 1/2/3 code -- it only validates a payload already assembled by
the caller.
"""

from __future__ import annotations

from typing import Any

CV_GENERATION_BASIS_VERSION = "cv-generation-basis.v1"
CV_DOCUMENT_MODEL_VERSION = "cv-document-model.v2"

_REQUIRED_SOURCE_ARTIFACT_TYPES = (
    "profile_snapshot",
    "job_fit_result",
    "resolved_job_evidence",
    "cv_content_plan",
    "cv_statement_plan",
)
_ARTIFACT_REF_KEYS = {"artifact_id", "artifact_type", "content_id"}
_REVIEW_KEYS = {
    "statement_plan_artifact_id",
    "consulted_decision_ids",
    "authorized_statement_ids",
    "omitted_statement_ids",
}
_TOP_LEVEL_KEYS = {"schema_version", "source_artifacts", "review", "cv_document_model"}


class CvGenerationBasisContractError(ValueError):
    pass


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CvGenerationBasisContractError(f"{label} must be an object")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CvGenerationBasisContractError(f"{label} must be a non-empty string")
    return value


def _require_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CvGenerationBasisContractError(f"{label} must be a list of strings")
    return value


def _validate_artifact_ref(value: Any, *, expected_artifact_type: str, label: str) -> dict[str, Any]:
    ref = _require_dict(value, label)
    if set(ref) != _ARTIFACT_REF_KEYS:
        raise CvGenerationBasisContractError(f"{label} has invalid keys")
    _require_nonempty_string(ref["artifact_id"], f"{label}.artifact_id")
    _require_nonempty_string(ref["content_id"], f"{label}.content_id")
    if ref["artifact_type"] != expected_artifact_type:
        raise CvGenerationBasisContractError(
            f"{label}.artifact_type must be {expected_artifact_type!r}, got {ref['artifact_type']!r}"
        )
    return ref


def validate_cv_generation_basis(basis: Any) -> dict[str, Any]:
    """Validate a ``cv-generation-basis.v1`` payload; raise on any defect.

    Checks structure, source-artifact refs, review-block consistency
    (no authorized/omitted overlap, model-selected IDs a subset of
    authorized IDs, statement-plan ref agreement), and that the embedded
    document model is exactly ``cv-document-model.v2``. Returns the payload
    unchanged on success.
    """

    payload = _require_dict(basis, "cv_generation_basis")
    if set(payload) != _TOP_LEVEL_KEYS:
        raise CvGenerationBasisContractError("cv_generation_basis has invalid top-level keys")
    if payload["schema_version"] != CV_GENERATION_BASIS_VERSION:
        raise CvGenerationBasisContractError(
            f"unsupported cv_generation_basis schema version {payload['schema_version']!r}; "
            f"expected {CV_GENERATION_BASIS_VERSION!r}"
        )

    source_artifacts = _require_dict(payload["source_artifacts"], "cv_generation_basis.source_artifacts")
    if set(source_artifacts) != set(_REQUIRED_SOURCE_ARTIFACT_TYPES):
        raise CvGenerationBasisContractError("cv_generation_basis.source_artifacts has invalid keys")
    for artifact_type in _REQUIRED_SOURCE_ARTIFACT_TYPES:
        _validate_artifact_ref(
            source_artifacts[artifact_type],
            expected_artifact_type=artifact_type,
            label=f"cv_generation_basis.source_artifacts.{artifact_type}",
        )

    review = _require_dict(payload["review"], "cv_generation_basis.review")
    if set(review) != _REVIEW_KEYS:
        raise CvGenerationBasisContractError("cv_generation_basis.review has invalid keys")
    _require_nonempty_string(review["statement_plan_artifact_id"], "cv_generation_basis.review.statement_plan_artifact_id")
    if review["statement_plan_artifact_id"] != source_artifacts["cv_statement_plan"]["artifact_id"]:
        raise CvGenerationBasisContractError(
            "cv_generation_basis.review.statement_plan_artifact_id does not match "
            "source_artifacts.cv_statement_plan.artifact_id"
        )
    consulted_decision_ids = _require_string_list(review["consulted_decision_ids"], "cv_generation_basis.review.consulted_decision_ids")
    authorized_ids = _require_string_list(review["authorized_statement_ids"], "cv_generation_basis.review.authorized_statement_ids")
    omitted_ids = _require_string_list(review["omitted_statement_ids"], "cv_generation_basis.review.omitted_statement_ids")
    if set(authorized_ids) & set(omitted_ids):
        raise CvGenerationBasisContractError(
            "cv_generation_basis.review.authorized_statement_ids and omitted_statement_ids overlap"
        )
    if len(consulted_decision_ids) != len(set(consulted_decision_ids)):
        raise CvGenerationBasisContractError("cv_generation_basis.review.consulted_decision_ids has duplicates")

    model = _require_dict(payload["cv_document_model"], "cv_generation_basis.cv_document_model")
    if model.get("schema_version") != CV_DOCUMENT_MODEL_VERSION:
        raise CvGenerationBasisContractError(
            f"cv_generation_basis.cv_document_model.schema_version must be {CV_DOCUMENT_MODEL_VERSION!r}, "
            f"got {model.get('schema_version')!r}"
        )
    model_selected_ids = model.get("provenance", {}).get("selected_statement_ids")
    if not isinstance(model_selected_ids, list) or any(not isinstance(item, str) for item in model_selected_ids):
        raise CvGenerationBasisContractError(
            "cv_generation_basis.cv_document_model.provenance.selected_statement_ids must be a list of strings"
        )
    if not set(model_selected_ids) <= set(authorized_ids):
        raise CvGenerationBasisContractError(
            "cv_generation_basis.cv_document_model contains statement IDs not present in "
            "review.authorized_statement_ids"
        )

    return payload
