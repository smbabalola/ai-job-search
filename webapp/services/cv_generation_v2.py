"""CV Generation Quality v2 planning orchestration (Phase 2B-1).

Sequences Task 1 (`product.cv_content_plan`) and Task 2
(`product.cv_statement_plan`) over the workspace's current immutable
Profile, Job Fit, and resolved-job-evidence artifacts, and persists their
exact outputs as first-class artifacts. This module makes no substantive
planning decisions itself -- those come entirely from product/*.

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


def _hash_artifact(prefix: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}{digest}"


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
    payloads, and persists each exact result unmodified as a new artifact
    (``cv_content_plan``, then ``cv_statement_plan``, the latter built from
    the former's own persisted payload). Never falls back to legacy CV
    generation and never touches Application Intelligence or Application
    Pack.
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
        content_plan_artifact = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_content_plan",
            payload=content_plan, content_id=_hash_artifact("cvcontentplan_", content_plan),
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

        statement_plan = build_cv_statement_plan(content_plan_artifact["payload"])
        statement_plan_artifact = save_artifact(
            conn, workspace_id=workspace_id, artifact_type="cv_statement_plan",
            payload=statement_plan, content_id=_hash_artifact("cvstatementplan_", statement_plan),
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
