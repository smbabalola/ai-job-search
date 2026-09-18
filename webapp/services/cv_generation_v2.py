"""CV Generation Quality v2 planning orchestration (Phase 2B-1 / 2B-1A).

Sequences Task 1 (`product.cv_content_plan`) and Task 2
(`product.cv_statement_plan`) over the workspace's current immutable
Profile, Job Fit, and resolved-job-evidence artifacts, and persists their
exact outputs as first-class artifacts. This module makes no substantive
planning decisions itself -- those come entirely from product/*.

Each persisted artifact is a thin provenance envelope around the untouched
Task 1/2 domain output: {schema_version, source_artifacts, <content_plan |
statement_plan>}. Task 1 and Task 2 themselves are never modified and never
see the envelope -- Task 2 is fed the unwrapped persisted content_plan, and
a later caller (Slice 2B-3) unwraps statement_plan the same way. The
envelope's source_artifacts are exact {artifact_id, artifact_type,
content_id} refs, matching the convention application_pack.py already
established; content_id hashes the *entire* persisted envelope (schema
version + source refs + domain payload), so two plans built from identical
domain content but different exact upstream artifact lineage are, correctly,
different persisted artifact states.

Explicitly out of scope for this slice: Task 3, Task 4, review-decision
authorization, Application Intelligence, and Application Pack. Those are
later Phase 2B slices.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from product.cv_content_plan import plan_cv_content
from product.cv_statement_plan import build_cv_statement_plan
from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID
from webapp.persistence.artifacts import get_current_artifact, save_artifact
from webapp.persistence.workspaces import get_profile_workspace_id
from webapp.services.pipeline import PipelineError
from webapp.services.staleness import record_dependency_fingerprint

CV_CONTENT_PLAN_ARTIFACT_VERSION = "cv-content-plan-artifact.v1"
CV_STATEMENT_PLAN_ARTIFACT_VERSION = "cv-statement-plan-artifact.v1"


def _hash_artifact(prefix: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}{digest}"


def _artifact_ref(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact["id"],
        "artifact_type": artifact["artifact_type"],
        "content_id": artifact["content_id"],
    }


def _current_or_error(
    conn: sqlite3.Connection, workspace_id: str, artifact_type: str,
    *, profile_workspace_id: str | None,
) -> dict[str, Any]:
    lookup_workspace = profile_workspace_id if artifact_type == "profile_snapshot" else workspace_id
    artifact = get_current_artifact(conn, lookup_workspace, artifact_type)
    if artifact is None:
        raise PipelineError(
            f"workspace {workspace_id} needs a current {artifact_type} to plan CV Quality v2 content"
        )
    return artifact


def plan_and_persist_cv_generation_v2(
    conn: sqlite3.Connection, workspace_id: str, *,
    account_id: str = DEFAULT_ACCOUNT_ID,
    budgets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run Task 1 then Task 2 over current immutable inputs and persist both.

    Loads the workspace's current ``profile_snapshot``, ``job_fit_result``,
    and ``resolved_job_evidence`` artifacts (erroring if any is missing),
    calls ``plan_cv_content`` then ``build_cv_statement_plan`` on their exact
    payloads, and persists each exact result -- wrapped in a thin provenance
    envelope pinning the exact upstream artifacts used -- as a new artifact
    (``cv_content_plan``, then ``cv_statement_plan``, the latter built from
    the former's own persisted, unwrapped ``content_plan``). Never falls back
    to legacy CV generation and never touches Application Intelligence or
    Application Pack.
    """

    try:
        conn.execute("BEGIN IMMEDIATE")
        profile_workspace_id = get_profile_workspace_id(conn, account_id)
        profile_artifact = _current_or_error(
            conn, workspace_id, "profile_snapshot", profile_workspace_id=profile_workspace_id,
        )
        job_fit_artifact = _current_or_error(
            conn, workspace_id, "job_fit_result", profile_workspace_id=profile_workspace_id,
        )
        resolved_job_evidence_artifact = _current_or_error(
            conn, workspace_id, "resolved_job_evidence", profile_workspace_id=profile_workspace_id,
        )

        content_plan = plan_cv_content(
            profile_artifact["payload"],
            job_fit_artifact["payload"],
            resolved_job_evidence_artifact["payload"],
            budgets=budgets,
        )
        content_plan_envelope = {
            "schema_version": CV_CONTENT_PLAN_ARTIFACT_VERSION,
            "source_artifacts": {
                "profile_snapshot": _artifact_ref(profile_artifact),
                "job_fit_result": _artifact_ref(job_fit_artifact),
                "resolved_job_evidence": _artifact_ref(resolved_job_evidence_artifact),
            },
            "content_plan": content_plan,
        }
        content_plan_artifact = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_content_plan",
            payload=content_plan_envelope, content_id=_hash_artifact("cvcontentplan_", content_plan_envelope),
            commit=False,
        )
        for upstream_type, upstream_artifact in (
            ("profile_snapshot", profile_artifact),
            ("job_fit_result", job_fit_artifact),
            ("resolved_job_evidence", resolved_job_evidence_artifact),
        ):
            record_dependency_fingerprint(
                conn, artifact_id=content_plan_artifact["id"],
                upstream_artifact_type=upstream_type,
                upstream_content_id=upstream_artifact["content_id"],
                commit=False,
            )

        statement_plan = build_cv_statement_plan(content_plan_artifact["payload"]["content_plan"])
        statement_plan_envelope = {
            "schema_version": CV_STATEMENT_PLAN_ARTIFACT_VERSION,
            "source_artifacts": {
                "cv_content_plan": _artifact_ref(content_plan_artifact),
            },
            "statement_plan": statement_plan,
        }
        statement_plan_artifact = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_statement_plan",
            payload=statement_plan_envelope,
            content_id=_hash_artifact("cvstatementplan_", statement_plan_envelope),
            commit=False,
        )
        record_dependency_fingerprint(
            conn, artifact_id=statement_plan_artifact["id"],
            upstream_artifact_type="cv_content_plan",
            upstream_content_id=content_plan_artifact["content_id"],
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "content_plan_artifact": content_plan_artifact,
        "statement_plan_artifact": statement_plan_artifact,
    }
