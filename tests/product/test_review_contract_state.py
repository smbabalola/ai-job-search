# tests/product/test_review_contract_state.py
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from product.review_contract import (
    ReviewSnapshot, ReviewWarning, WarningLevel, approval_binding, binding_hash, derive_review_state,
)
from tests.product.test_review_contract_binding import reviewable

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
TTL = timedelta(days=14)


def snap(r="default", approval="match", **kw):
    r = reviewable() if r == "default" else r
    latest = None
    if approval == "match":
        latest = {"binding_hash": binding_hash(approval_binding(r)), "created_at": NOW, "revoked": False}
    elif approval == "other":
        latest = {"binding_hash": "sha256:old", "created_at": NOW, "revoked": False}
    values = dict(reviewable=r, workflow_status="drafted", latest_approval=latest, open_delta_keys=(), now=NOW,
                  ttl=TTL)
    values.update(kw)
    return ReviewSnapshot(**values)


def test_effective_approval_is_approved_for_fill():
    s = derive_review_state(snap())
    assert (s.state, s.binding_matches, s.approval_effective) == ("APPROVED_FOR_FILL", True, True)


@pytest.mark.parametrize("case,reason", [
    ("blocking", "blocking_issues"), ("unacked", "unacknowledged_attention"), ("delta", "open_deltas"),
    ("revoked", "revoked"), ("expired", "expired")])
def test_binding_can_match_while_approval_is_not_effective(case, reason):
    r = reviewable()
    if case == "blocking":
        r = reviewable(warnings=r.warnings + (ReviewWarning("b", WarningLevel.BLOCKING, "x", False),))
    if case == "unacked":
        r = reviewable(warnings=(ReviewWarning("user_managed:cover_letter", WarningLevel.ATTENTION, "n", False),))
    s0 = snap(r, open_delta_keys=("subject:salary",) if case == "delta" else ())
    if case == "revoked":
        s0 = replace(s0, latest_approval={**s0.latest_approval, "revoked": True})
    if case == "expired":
        s0 = replace(s0, now=NOW + TTL)
    s = derive_review_state(s0)
    assert s.binding_matches and not s.approval_effective and s.state == "NEEDS_REVIEW" and reason in s.reasons


def test_ttl_boundary_is_inclusive():
    assert derive_review_state(snap(now=NOW + TTL - timedelta(microseconds=1))).approval_effective
    assert not derive_review_state(snap(now=NOW + TTL)).approval_effective


def test_other_states():
    assert derive_review_state(snap(approval=None)).state == "READY_FOR_REVIEW"
    assert derive_review_state(snap(approval="other")).reasons[0] == "binding_changed"
    assert derive_review_state(snap(workflow_status="applied")).state == "CLOSED"
    assert derive_review_state(snap(r=None, approval=None)).state == "NOT_READY"


def test_no_exact_pack_exposes_no_binding_hash():
    s = derive_review_state(snap(exact_pack=False))
    assert s.binding_hash is None and s.provisional_hash and not s.approval_effective


def test_undecided_optional_and_unanswered_required_fields_block():
    base = reviewable()
    undecided = replace(base, fields=(replace(base.fields[1], disposition=None), base.fields[0]))
    required_blank = replace(base, fields=(replace(base.fields[0], disposition="OMIT"), base.fields[1]))
    assert "field_undecided:subject:relocate" in derive_review_state(snap(undecided, approval=None)).blocking
    assert "field_unanswered:subject:notice_period" in derive_review_state(snap(required_blank, approval=None)).blocking
