"""Pure projection of a review-authorized CV Generation Quality v2 statement plan.

This module makes no review decisions and performs no persistence or artifact
lookups. It answers one narrow question: given an exact Task 2
``cv-statement-plan`` payload and the exact set of effective review decisions
already resolved for it, which statements are render-authorized, and what does
a Task-3-acceptable statement plan look like once only those statements remain?

Authorization binds strictly to ``(source_artifact_id, statement_id)`` --
never to statement text, position, or section. This module has no way to
resolve *effective* decisions itself (that requires the review persistence
layer's latest-decision-wins rule); callers must supply the already-resolved
mapping.
"""

from __future__ import annotations

import copy
from typing import Any

CV_STATEMENT_REVIEW_ITEM_TYPE = "cv_statement_v2"
AUTHORIZED_DISPOSITION = "acknowledged_and_proceed"


class CvReviewProjectionError(RuntimeError):
    pass


def reviewable_cv_statements(statement_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the exact Task 2 statements that require human review.

    Every entry in ``statement_plan["statements"]`` is already candidate-facing:
    Task 2 only emits a statement when it has real, evidence-grounded text.
    Omitted/ungroupable evidence and uncovered requirements are separate
    diagnostic collections that were never rendered and never need review.
    """

    statements = statement_plan.get("statements", [])
    if not isinstance(statements, list):
        raise CvReviewProjectionError("cv_statement_plan.statements must be a list")
    return [
        statement for statement in statements
        if isinstance(statement, dict) and isinstance(statement.get("statement_id"), str)
    ]


def project_review_authorized_statement_plan(
    statement_plan: dict[str, Any],
    effective_decisions: dict[str, dict[str, Any]],
    *,
    source_artifact_id: str,
) -> dict[str, Any]:
    """Return a statement plan containing only render-authorized statements.

    ``effective_decisions`` maps an exact ``statement_id`` to its already-
    resolved effective decision row (latest-decision-wins already applied by
    the caller) for the exact ``source_artifact_id`` plan. A statement id with
    no entry is treated as unreviewed, never as authorized.

    ``source_artifact_id`` is not merely used for error messages: every
    supplied decision must itself carry this exact ``source_artifact_id``
    (every real review_decisions row does). A decision whose own
    source_artifact_id differs -- e.g. a caller accidentally merging
    decisions from two different statement-plan artifacts into one map --
    is rejected here rather than silently trusted, so this function enforces
    artifact-identity binding itself and does not merely rely on its caller
    having filtered correctly beforehand.

    Fails closed: every reviewable statement in ``statement_plan`` must have
    an effective decision (of any disposition) before a projection can be
    returned at all -- review completion is required even though a rejected
    statement is simply excluded rather than raising.
    """

    reviewable = reviewable_cv_statements(statement_plan)
    reviewable_ids = {statement["statement_id"] for statement in reviewable}

    mismatched = sorted(
        statement_id for statement_id, decision in effective_decisions.items()
        if decision.get("source_artifact_id") != source_artifact_id
    )
    if mismatched:
        raise CvReviewProjectionError(
            f"decision(s) for statement(s) {mismatched} do not belong to "
            f"cv statement plan {source_artifact_id!r}"
        )

    missing = sorted(reviewable_ids - set(effective_decisions))
    if missing:
        raise CvReviewProjectionError(
            f"cv statement plan {source_artifact_id!r} has unreviewed statements: {missing}"
        )

    authorized_ids = {
        statement_id for statement_id, decision in effective_decisions.items()
        if statement_id in reviewable_ids
        and decision.get("disposition") == AUTHORIZED_DISPOSITION
    }

    projected = copy.deepcopy(statement_plan)
    projected["statements"] = [
        copy.deepcopy(statement) for statement in reviewable
        if statement["statement_id"] in authorized_ids
    ]
    return projected
