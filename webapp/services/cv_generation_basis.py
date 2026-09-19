"""Immutable reviewed CV Generation Quality v2 basis (Phase 2B-3).

Builds and persists a ``cv_generation_basis`` artifact: the exact, reviewed
input Task 4 will eventually render. This module calls the unchanged Task 3
builder (``product.cv_document_model.build_cv_document_model``) exactly once,
over the exact review-authorized projection obtained from the frozen Slice
2B-2 service, and pins the exact upstream lineage recovered from the Slice
2B-1A provenance envelopes -- never from "current artifact" lookups, never
from content-ID guessing.

Explicitly out of scope for this slice: Task 4 (DOCX rendering), document
blob storage, document selection, and Application Pack confirmation.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from product.cv_document_model import build_cv_document_model
from product.cv_generation_basis_contract import (
    CV_GENERATION_BASIS_VERSION,
    validate_cv_generation_basis,
)
from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID
from webapp.persistence.artifacts import get_artifact, save_artifact
from webapp.services.cv_generation_v2 import CV_CONTENT_PLAN_ARTIFACT_VERSION, CV_STATEMENT_PLAN_ARTIFACT_VERSION
from webapp.services.cv_statement_review import get_review_authorized_cv_statement_plan
from webapp.services.pipeline import PipelineError
from webapp.services.staleness import record_dependency_fingerprint


class CvGenerationBasisError(PipelineError):
    pass


def _hash_payload(prefix: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}{digest}"


def _load_exact_artifact(
    conn: sqlite3.Connection, artifact_id: str, *, expected_artifact_type: str,
) -> dict[str, Any]:
    """Load one artifact by its exact, caller-supplied ID. Never a current-artifact lookup."""

    artifact = get_artifact(conn, artifact_id)
    if artifact is None:
        raise CvGenerationBasisError(f"artifact {artifact_id!r} not found")
    if artifact["artifact_type"] != expected_artifact_type:
        raise CvGenerationBasisError(
            f"artifact {artifact_id!r} is not a {expected_artifact_type} "
            f"(got {artifact['artifact_type']!r})"
        )
    return artifact


def _verify_ref_matches(ref: dict[str, Any], artifact: dict[str, Any], *, label: str) -> None:
    """Fail closed if a lineage ref's artifact_id resolves to a row whose own
    type/content_id disagree with what the ref claims -- never trust the ID
    alone."""

    if ref["artifact_type"] != artifact["artifact_type"]:
        raise CvGenerationBasisError(f"{label}: artifact_type mismatch against the resolved artifact row")
    if ref["content_id"] != artifact["content_id"]:
        raise CvGenerationBasisError(f"{label}: content_id mismatch against the resolved artifact row")


def build_and_persist_cv_generation_basis(
    conn: sqlite3.Connection, workspace_id: str, *,
    statement_plan_artifact_id: str, account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any]:
    """Build and persist the exact reviewed cv_generation_basis for a pinned plan.

    ``statement_plan_artifact_id`` must be the exact ``cv_statement_plan``
    artifact to build from; this function never substitutes the workspace's
    current statement plan in its place. Fails closed (raises, persists
    nothing) if review is incomplete, if any lineage ref fails to verify
    against its resolved artifact row, or if Task 3's output contains a
    statement ID outside the authorized set.
    """

    try:
        conn.execute("BEGIN IMMEDIATE")

        statement_plan_artifact = _load_exact_artifact(
            conn, statement_plan_artifact_id, expected_artifact_type="cv_statement_plan",
        )
        statement_envelope = statement_plan_artifact["payload"]
        if (
            not isinstance(statement_envelope, dict)
            or statement_envelope.get("schema_version") != CV_STATEMENT_PLAN_ARTIFACT_VERSION
        ):
            raise CvGenerationBasisError(f"cv_statement_plan artifact {statement_plan_artifact_id!r} is malformed")

        content_plan_ref = statement_envelope.get("source_artifacts", {}).get("cv_content_plan")
        if not isinstance(content_plan_ref, dict):
            raise CvGenerationBasisError(
                f"cv_statement_plan artifact {statement_plan_artifact_id!r} is missing its cv_content_plan lineage ref"
            )
        content_plan_artifact = _load_exact_artifact(
            conn, content_plan_ref["artifact_id"], expected_artifact_type="cv_content_plan",
        )
        _verify_ref_matches(
            content_plan_ref, content_plan_artifact,
            label=f"cv_statement_plan {statement_plan_artifact_id!r} source_artifacts.cv_content_plan",
        )

        content_envelope = content_plan_artifact["payload"]
        if (
            not isinstance(content_envelope, dict)
            or content_envelope.get("schema_version") != CV_CONTENT_PLAN_ARTIFACT_VERSION
        ):
            raise CvGenerationBasisError(f"cv_content_plan artifact {content_plan_ref['artifact_id']!r} is malformed")

        upstream_refs: dict[str, dict[str, Any]] = {}
        upstream_artifacts: dict[str, dict[str, Any]] = {}
        for upstream_type in ("profile_snapshot", "job_fit_result", "resolved_job_evidence"):
            ref = content_envelope.get("source_artifacts", {}).get(upstream_type)
            if not isinstance(ref, dict):
                raise CvGenerationBasisError(
                    f"cv_content_plan artifact {content_plan_ref['artifact_id']!r} is missing "
                    f"its {upstream_type} lineage ref"
                )
            artifact = _load_exact_artifact(conn, ref["artifact_id"], expected_artifact_type=upstream_type)
            _verify_ref_matches(
                ref, artifact,
                label=f"cv_content_plan {content_plan_ref['artifact_id']!r} source_artifacts.{upstream_type}",
            )
            upstream_refs[upstream_type] = ref
            upstream_artifacts[upstream_type] = artifact

        review_result = get_review_authorized_cv_statement_plan(
            conn, workspace_id, statement_plan_artifact_id=statement_plan_artifact_id, account_id=account_id,
        )
        projected_statement_plan = review_result["projected_statement_plan"]

        # Order authorized/omitted IDs, and their corresponding decision IDs,
        # by Task 2's own deterministic statement order -- never by SQLite
        # row order or plain lexicographic string sort.
        task2_order = [
            statement["statement_id"]
            for statement in statement_envelope["statement_plan"].get("statements", [])
        ]
        authorized_set = set(review_result["authorized_statement_ids"])
        omitted_set = set(review_result["omitted_statement_ids"])
        authorized_statement_ids = [sid for sid in task2_order if sid in authorized_set]
        omitted_statement_ids = [sid for sid in task2_order if sid in omitted_set]
        consulted_decision_ids = sorted(review_result["consulted_decision_ids"])

        cv_document_model = build_cv_document_model(projected_statement_plan)

        model_selected_ids = set(cv_document_model["provenance"]["selected_statement_ids"])
        if not model_selected_ids <= authorized_set:
            raise CvGenerationBasisError(
                "cv_document_model contains statement IDs that are not authorized -- "
                "refusing to persist a generation basis with unauthorized content"
            )
        if not model_selected_ids <= set(task2_order):
            raise CvGenerationBasisError(
                "cv_document_model references statement IDs absent from the pinned cv_statement_plan artifact"
            )

        basis_payload = {
            "schema_version": CV_GENERATION_BASIS_VERSION,
            "source_artifacts": {
                "profile_snapshot": upstream_refs["profile_snapshot"],
                "job_fit_result": upstream_refs["job_fit_result"],
                "resolved_job_evidence": upstream_refs["resolved_job_evidence"],
                "cv_content_plan": content_plan_ref,
                "cv_statement_plan": {
                    "artifact_id": statement_plan_artifact["id"],
                    "artifact_type": "cv_statement_plan",
                    "content_id": statement_plan_artifact["content_id"],
                },
            },
            "review": {
                "statement_plan_artifact_id": statement_plan_artifact_id,
                "consulted_decision_ids": consulted_decision_ids,
                "authorized_statement_ids": authorized_statement_ids,
                "omitted_statement_ids": omitted_statement_ids,
            },
            "cv_document_model": cv_document_model,
        }
        validate_cv_generation_basis(basis_payload)

        basis_artifact = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_generation_basis",
            payload=basis_payload, content_id=_hash_payload("cvgenbasis_", basis_payload),
            commit=False,
        )
        record_dependency_fingerprint(
            conn, artifact_id=basis_artifact["id"],
            upstream_artifact_type="cv_statement_plan",
            upstream_content_id=statement_plan_artifact["content_id"],
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"basis_artifact": basis_artifact}
