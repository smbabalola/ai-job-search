"""Bundle 6D-A Task 5: Reach.APPLICATION and the applicability / readiness
rules shared by the 6B gate and Review."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from product import autonomy_gate
from product.autonomy_contract import (
    REACH_ORDER, AnswerCandidate, EmployerKeyStrength, Reach, RepresentationRequirement,
)
from product.autonomy_gate import answer_readiness, usable_answer_candidates
from product.semantic_subject_policy import load_subject_policy, subject_entry

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
SUBJECT = "employment.notice_period"  # freshness 60 days, max reach ACCOUNT


def cand(reach, scope_id, *, confirmed_at=NOW, basis_kind="USER_ASSERTION", at=None, current=None, answer_id="a1",
         context=None, contradicted=False):
    return AnswerCandidate(approved_answer_id=answer_id, subject=SUBJECT, reach=reach, scope_id=scope_id,
                           context=context or {}, confirmed_at=confirmed_at, basis_kind=basis_kind,
                           basis_hash_at_approval=at, basis_hash_current=current, contradicted=contradicted)


def entry(subject=SUBJECT):
    return subject_entry(load_subject_policy(), subject)


def usable(cands, ws="ws_a"):
    req = RepresentationRequirement(key="subject:" + SUBJECT, subject=SUBJECT, required=True,
                                    evidence_available=False, candidates=tuple(cands))
    return usable_answer_candidates(req, entry(), application_workspace_id=ws, search_workspace_id="sw_1",
                                    employer_key="acme", employer_key_strength=EmployerKeyStrength.NORMALIZED_NAME)


def test_application_reach_applies_only_to_its_own_application():
    mine = cand(Reach.APPLICATION, "ws_a", answer_id="mine")
    other = cand(Reach.APPLICATION, "ws_b", answer_id="other")
    found, contradicted = usable([mine, other], ws="ws_a")
    assert [c.approved_answer_id for c in found] == ["mine"] and not contradicted


def test_reach_order_keeps_existing_relative_order():
    assert REACH_ORDER[Reach.APPLICATION] < REACH_ORDER[Reach.EMPLOYER] < REACH_ORDER[Reach.SEARCH_WORKSPACE] \
        < REACH_ORDER[Reach.ACCOUNT]


def test_contradicted_candidates_are_flagged():
    _, contradicted = usable([cand(Reach.ACCOUNT, "acct", contradicted=True)])
    assert contradicted


def test_answer_readiness_boundary_matches_the_gate():
    days = entry()["freshness_days"]
    at_boundary = cand(Reach.ACCOUNT, "acct", confirmed_at=NOW - timedelta(days=days))
    past = cand(Reach.ACCOUNT, "acct", confirmed_at=NOW - timedelta(days=days, microseconds=1))
    assert not answer_readiness(at_boundary, entry(), NOW).expired
    assert answer_readiness(past, entry(), NOW).expired
    assert answer_readiness(at_boundary, entry(), NOW).expires_at == NOW


@pytest.mark.parametrize("at,current,state", [("h1", "h1", "ok"), ("h1", "h2", "stale"), ("h1", None, "missing")])
def test_answer_readiness_basis_state(at, current, state):
    c = cand(Reach.ACCOUNT, "acct", basis_kind="EVIDENCE", at=at, current=current)
    assert answer_readiness(c, entry(), NOW).basis == state


def test_gate_submit_blocker_uses_answer_readiness(monkeypatch):
    calls = []
    real = autonomy_gate.answer_readiness

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(autonomy_gate, "answer_readiness", spy)
    req = RepresentationRequirement(key="k", subject=SUBJECT, required=True, evidence_available=False)
    assert autonomy_gate._submit_blocker(cand(Reach.ACCOUNT, "acct"), req, entry(), NOW) in (
        None, "profile_basis_unverifiable")
    assert calls
