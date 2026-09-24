"""CV Generation Quality v2 statement review service (Phase 2B-2).

Reuses the existing generic review_decisions persistence layer unchanged --
no new table, no new schema, no new decision-resolution rule. The exact
"newest decision per (review_item_type, domain_item_id) wins" semantics
already established in webapp.services.application_pack are the sole source
of effective-decision truth here.

This module makes no Task 3 calls and persists no generation basis. It only:
  (A) enumerates the review items a reviewer must decide on for an exact
      cv_statement_plan artifact;
  (B) validates and saves one review decision against that exact artifact;
  (C) resolves effective review state (authorized / omitted / pending) for an
      exact artifact;
  (D) exposes a fail-closed accessor returning the review-authorized
      projection of an exact, caller-pinned cv_statement_plan artifact.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from product.cv_review_projection import (
    AUTHORIZED_DISPOSITION,
    CV_STATEMENT_REVIEW_ITEM_TYPE,
    project_review_authorized_statement_plan,
    reviewable_cv_statements,
)
from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID
from webapp.persistence.artifacts import get_artifact
from webapp.persistence.review import DISPOSITIONS, list_review_decisions, save_review_decision
from webapp.services.cv_generation_v2 import CV_STATEMENT_PLAN_ARTIFACT_VERSION
from webapp.services.pipeline import PipelineError


class CvStatementReviewError(PipelineError):
    pass


def _load_statement_plan(conn: sqlite3.Connection, statement_plan_artifact_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load and validate the exact pinned envelope; return (artifact, inner statement_plan).

    The review binding identity (source_artifact_id for every decision) is
    always the outer artifact's own id -- never anything from the inner,
    unwrapped Task 2 payload.
    """

    artifact = get_artifact(conn, statement_plan_artifact_id)
    if artifact is None:
        raise CvStatementReviewError(f"cv_statement_plan artifact {statement_plan_artifact_id!r} not found")
    if artifact["artifact_type"] != "cv_statement_plan":
        raise CvStatementReviewError(
            f"artifact {statement_plan_artifact_id!r} is not a cv_statement_plan "
            f"(got {artifact['artifact_type']!r})"
        )
    envelope = artifact["payload"]
    if not isinstance(envelope, dict) or envelope.get("schema_version") != CV_STATEMENT_PLAN_ARTIFACT_VERSION:
        raise CvStatementReviewError(
            f"cv_statement_plan artifact {statement_plan_artifact_id!r} is malformed"
        )
    statement_plan = envelope.get("statement_plan")
    if not isinstance(statement_plan, dict):
        raise CvStatementReviewError(
            f"cv_statement_plan artifact {statement_plan_artifact_id!r} is malformed"
        )
    return artifact, statement_plan


def _effective_decisions(
    conn: sqlite3.Connection, workspace_id: str, statement_plan_artifact_id: str,
) -> dict[str, dict[str, Any]]:
    """Newest decision per exact stmt_* domain_item_id, for this exact artifact only.

    Mirrors webapp.services.application_pack._decision_index exactly:
    list_review_decisions already orders rows newest-first, so the first
    occurrence of each domain_item_id via setdefault is the effective one.
    """

    indexed: dict[str, dict[str, Any]] = {}
    for decision in list_review_decisions(conn, workspace_id, statement_plan_artifact_id):
        if decision["review_item_type"] != CV_STATEMENT_REVIEW_ITEM_TYPE:
            continue
        domain_item_id = decision["domain_item_id"]
        if isinstance(domain_item_id, str):
            indexed.setdefault(domain_item_id, decision)
    return indexed


def list_cv_statement_review_items(
    conn: sqlite3.Connection, statement_plan_artifact_id: str,
) -> list[dict[str, Any]]:
    """Return the exact reviewable statements for a pinned cv_statement_plan artifact.

    Deterministic Task 2 order. Exposes only statement_id and the exact
    candidate-facing text -- not internal evidence/provenance metadata.
    """

    _artifact, statement_plan = _load_statement_plan(conn, statement_plan_artifact_id)
    statements = reviewable_cv_statements(statement_plan)
    return [
        {"statement_id": statement["statement_id"], "statement_text": statement.get("statement_text")}
        for statement in statements
    ]


def save_cv_statement_review_decision(
    conn: sqlite3.Connection, workspace_id: str, *,
    statement_plan_artifact_id: str, statement_id: str, disposition: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Validate and persist one review decision against an exact statement.

    Rejects: nonexistent/wrong-typed source artifact, malformed plan, unknown
    statement_id, and any disposition outside the existing review vocabulary.
    Delegates the actual write to the existing generic review persistence
    layer unchanged.
    """

    _artifact, statement_plan = _load_statement_plan(conn, statement_plan_artifact_id)
    reviewable_ids = {
        statement["statement_id"] for statement in reviewable_cv_statements(statement_plan)
    }
    if statement_id not in reviewable_ids:
        raise CvStatementReviewError(
            f"statement {statement_id!r} is not a reviewable statement of "
            f"cv_statement_plan artifact {statement_plan_artifact_id!r}"
        )
    if disposition not in DISPOSITIONS:
        raise CvStatementReviewError(f"unknown review disposition: {disposition!r}")

    return save_review_decision(
        conn, workspace_id=workspace_id, review_item_type=CV_STATEMENT_REVIEW_ITEM_TYPE,
        source_artifact_id=statement_plan_artifact_id, domain_item_id=statement_id,
        disposition=disposition, note=note,
    )


def resolve_cv_statement_review_state(
    conn: sqlite3.Connection, workspace_id: str, statement_plan_artifact_id: str,
) -> dict[str, list[str]]:
    """Return {authorized, omitted, pending} exact statement_id sets for this artifact."""

    _artifact, statement_plan = _load_statement_plan(conn, statement_plan_artifact_id)
    reviewable_ids = [
        statement["statement_id"] for statement in reviewable_cv_statements(statement_plan)
    ]
    effective = _effective_decisions(conn, workspace_id, statement_plan_artifact_id)

    authorized: list[str] = []
    omitted: list[str] = []
    pending: list[str] = []
    for statement_id in reviewable_ids:
        decision = effective.get(statement_id)
        if decision is None:
            pending.append(statement_id)
        elif decision["disposition"] == AUTHORIZED_DISPOSITION:
            authorized.append(statement_id)
        else:
            omitted.append(statement_id)
    return {"authorized": authorized, "omitted": omitted, "pending": pending}


def get_review_authorized_cv_statement_plan(
    conn: sqlite3.Connection, workspace_id: str, *,
    statement_plan_artifact_id: str, account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any]:
    """Fail-closed accessor: the exact reviewed projection of a pinned artifact.

    The caller supplies the exact ``statement_plan_artifact_id`` to project;
    this function never substitutes ``get_current_artifact(..., "cv_statement_plan")``
    in its place, so a caller reviewing an older plan can never be silently
    handed a newer, differently-reviewed one.

    Raises CvReviewProjectionError if any reviewable statement in the exact
    artifact lacks an effective decision (fails closed on incomplete review).
    """

    _artifact, statement_plan = _load_statement_plan(conn, statement_plan_artifact_id)
    effective = _effective_decisions(conn, workspace_id, statement_plan_artifact_id)

    projected_plan = project_review_authorized_statement_plan(
        statement_plan, effective, source_artifact_id=statement_plan_artifact_id,
    )
    authorized_statement_ids = sorted(
        statement["statement_id"] for statement in projected_plan["statements"]
    )
    omitted_statement_ids = sorted(
        statement_id for statement_id, decision in effective.items()
        if decision["disposition"] != AUTHORIZED_DISPOSITION
    )
    consulted_decision_ids = sorted(decision["id"] for decision in effective.values())

    return {
        "statement_plan_artifact_id": statement_plan_artifact_id,
        "projected_statement_plan": projected_plan,
        "authorized_statement_ids": authorized_statement_ids,
        "omitted_statement_ids": omitted_statement_ids,
        "consulted_decision_ids": consulted_decision_ids,
    }
