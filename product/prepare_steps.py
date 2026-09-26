"""Pure Bundle 6C preparation logic (spec §8.1, §8.2, §10.2). No IO: the
webapp builds the snapshot from authoritative artifacts and decisions."""
from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from product.autonomy_contract import canonical_hash


class StepKind(str, Enum):
    EVALUATE = "EVALUATE"
    UNDERSTAND = "UNDERSTAND"
    FIT = "FIT"
    INTELLIGENCE = "INTELLIGENCE"
    SYSTEM_REVIEW = "SYSTEM_REVIEW"
    GATE4 = "GATE4"


PAID_STEPS = frozenset({StepKind.EVALUATE, StepKind.UNDERSTAND, StepKind.FIT, StepKind.INTELLIGENCE})
POST_DRAFT_STATUSES = frozenset({"applied", "interview", "offer", "hired", "rejected", "no_response",
                                 "offer_declined", "withdrawn"})


@dataclass(frozen=True)
class PrepareSnapshot:
    workflow_status: str | None
    profile_available: bool
    understanding_current: bool
    fit_current: bool
    intelligence_current: bool
    pack_current: bool
    mechanically_acceptable: int
    judgment_outstanding: int
    latched: bool
    # The enabled document path needs a human file selection before Gate 4
    # (CV Quality v2); the system never confirms another path instead.
    document_selection_required: bool = False


@dataclass(frozen=True)
class NextStep:
    kind: str  # RUN | DONE | PREPARED | NEEDS_USER
    step: StepKind | None = None
    reason: str = ""


def next_prepare_step(s: PrepareSnapshot) -> NextStep:
    """First match wins (spec §8.1, with DONE before the profile check)."""
    if s.workflow_status in POST_DRAFT_STATUSES:
        return NextStep("DONE", reason="submitted")
    if not s.profile_available:
        return NextStep("NEEDS_USER", reason="profile_missing")
    if not s.understanding_current:
        return NextStep("RUN", StepKind.UNDERSTAND)
    if not s.fit_current:
        return NextStep("RUN", StepKind.FIT)
    if not s.intelligence_current:
        return NextStep("RUN", StepKind.INTELLIGENCE)
    if s.pack_current:
        return NextStep("PREPARED")
    if s.latched:
        return NextStep("NEEDS_USER", reason="human_review_latched")
    if s.mechanically_acceptable:
        return NextStep("RUN", StepKind.SYSTEM_REVIEW)
    if s.judgment_outstanding:
        return NextStep("NEEDS_USER", reason="pack_review")
    if s.document_selection_required:
        return NextStep("NEEDS_USER", reason="document_selection_required")
    return NextStep("RUN", StepKind.GATE4)


# ---- mechanical review ------------------------------------------------------

@dataclass(frozen=True)
class SystemReviewVerdict:
    item_type: str
    item_id: str
    source_artifact_id: str
    reason: str
    item_content_hash: str


def _float_safe(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _float_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_float_safe(v) for v in value]
    return value


def item_content_hash(item: Mapping[str, Any]) -> str:
    return canonical_hash("autonomy-review-item", "v1", _float_safe({
        "item_type": item["item_type"], "item_id": item["item_id"],
        "source_artifact_id": item["source_artifact_id"], "source": item["source"],
    }))


def grounded_claim_ids(profile: Mapping[str, Any]) -> frozenset[str]:
    conflicted = {c.get("concept_id") for c in profile.get("conflicts", [])}
    return frozenset(
        claim["id"] for claim in profile.get("claims", [])
        if not claim.get("placeholder") and claim.get("concept_id") not in conflicted
    )


def mechanical_review(item: Mapping[str, Any], profile: Mapping[str, Any]) -> SystemReviewVerdict | None:
    """Only a READY content unit whose every cited claim is in the current
    bound profile, not a placeholder and not conflicted, is system-confirmable.
    Everything else is a judgment item and stays undecided (returns None)."""
    if item.get("item_type") != "content_unit":
        return None
    source = item.get("source") or {}
    evidence = source.get("profile_evidence_ids") or []
    if source.get("status") != "READY" or not source.get("text") or not evidence:
        return None
    if not set(evidence) <= grounded_claim_ids(profile):
        return None
    return SystemReviewVerdict(
        item_type=item["item_type"], item_id=item["item_id"], source_artifact_id=item["source_artifact_id"],
        reason="grounded_ready_unit", item_content_hash=item_content_hash(item),
    )


def pack_revision(profile_content_id: str, fit_content_id: str, intelligence_content_id: str) -> str:
    return canonical_hash("autonomy-pack-revision", "v1", {
        "profile_snapshot": profile_content_id, "job_fit_result": fit_content_id,
        "application_intelligence_result": intelligence_content_id,
    })


# ---- retry policy -------------------------------------------------------------

class ErrorClass(str, Enum):
    TRANSIENT = "TRANSIENT"
    HUMAN_FIXABLE = "HUMAN_FIXABLE"
    INTERNAL = "INTERNAL"


MAX_ATTEMPTS_PER_CYCLE = 4  # the initial attempt plus 3 automatic retries


def retry_delay_seconds(failures_in_cycle: int, delays: Sequence[float], rng: random.Random,
                        retry_after: float | None = None) -> float | None:
    """Delay before the next attempt after `failures_in_cycle` consecutive
    TRANSIENT failures, or None when the cycle is exhausted (escalate).
    ±20 % jitter from the injected rng; Retry-After never shortens it."""
    if failures_in_cycle < 1 or failures_in_cycle > len(delays):
        return None
    jittered = delays[failures_in_cycle - 1] * (0.8 + 0.4 * rng.random())
    return max(jittered, retry_after or 0.0)
