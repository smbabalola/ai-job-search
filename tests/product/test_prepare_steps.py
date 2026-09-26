from __future__ import annotations

import random
from decimal import Decimal

import pytest
from hypothesis import given, settings, strategies as st

from product.prepare_steps import (
    MAX_ATTEMPTS_PER_CYCLE, NextStep, PrepareSnapshot, StepKind, grounded_claim_ids, item_content_hash,
    mechanical_review, next_prepare_step, pack_revision, retry_delay_seconds,
)

BASE = dict(workflow_status=None, profile_available=True, understanding_current=True, fit_current=True,
            intelligence_current=True, pack_current=False, mechanically_acceptable=0, judgment_outstanding=0,
            latched=False)


def snap(**kw):
    return PrepareSnapshot(**{**BASE, **kw})


@pytest.mark.parametrize("kw, expected", [
    (dict(workflow_status="applied", profile_available=False), NextStep("DONE", None, "submitted")),
    (dict(profile_available=False), NextStep("NEEDS_USER", None, "profile_missing")),
    (dict(understanding_current=False, fit_current=False), NextStep("RUN", StepKind.UNDERSTAND)),
    (dict(fit_current=False, intelligence_current=False), NextStep("RUN", StepKind.FIT)),
    (dict(intelligence_current=False), NextStep("RUN", StepKind.INTELLIGENCE)),
    (dict(pack_current=True, mechanically_acceptable=2), NextStep("PREPARED")),
    (dict(mechanically_acceptable=2, judgment_outstanding=1), NextStep("RUN", StepKind.SYSTEM_REVIEW)),
    (dict(judgment_outstanding=1), NextStep("NEEDS_USER", None, "pack_review")),
    (dict(latched=True, mechanically_acceptable=2), NextStep("NEEDS_USER", None, "human_review_latched")),
    (dict(latched=True), NextStep("NEEDS_USER", None, "human_review_latched")),
    (dict(), NextStep("RUN", StepKind.GATE4)),
])
def test_next_step_order(kw, expected):
    got = next_prepare_step(snap(**kw))
    assert (got.kind, got.step) == (expected.kind, expected.step)
    if expected.reason:
        assert got.reason == expected.reason


def test_done_is_checked_before_profile():
    for status in ("applied", "interview", "offer", "hired", "rejected", "no_response", "offer_declined", "withdrawn"):
        assert next_prepare_step(snap(workflow_status=status, profile_available=False)).kind == "DONE"
    assert next_prepare_step(snap(workflow_status="drafted")).kind == "RUN"  # drafted is still preparable


PROFILE = {
    "claims": [
        {"id": "clm_ok", "concept_id": "cpt_a", "placeholder": False},
        {"id": "clm_ph", "concept_id": "cpt_b", "placeholder": True},
        {"id": "clm_cf", "concept_id": "cpt_c", "placeholder": False},
    ],
    "conflicts": [{"id": "cf1", "concept_id": "cpt_c"}],
}


def unit(**kw):
    src = {"unit_id": "u1", "status": "READY", "text": "Led drilling fluids programmes.",
           "profile_evidence_ids": ["clm_ok"], **kw}
    return {"item_type": "content_unit", "item_id": "u1", "source_artifact_id": "art_i", "source": src}


def test_grounded_claims_exclude_placeholders_and_conflicts():
    assert grounded_claim_ids(PROFILE) == frozenset({"clm_ok"})


def test_only_grounded_ready_units_are_accepted():
    verdict = mechanical_review(unit(), PROFILE)
    assert verdict and verdict.reason == "grounded_ready_unit"
    assert verdict.item_content_hash == item_content_hash(unit())
    for bad in (unit(status="NEEDS_REVIEW"), unit(text=""), unit(profile_evidence_ids=[]),
                unit(profile_evidence_ids=["clm_ph"]), unit(profile_evidence_ids=["clm_cf"]),
                unit(profile_evidence_ids=["clm_ok", "clm_missing"])):
        assert mechanical_review(bad, PROFILE) is None


@settings(max_examples=200, deadline=None, derandomize=True)
@given(item_type=st.sampled_from(["functionally_equivalent_match", "transferable_match", "gate_flag",
                                  "human_judgment_question", "profile_conflict", "profile_placeholder"]))
def test_judgment_item_types_are_never_accepted(item_type):
    item = {**unit(), "item_type": item_type}
    assert mechanical_review(item, PROFILE) is None


@settings(max_examples=200, deadline=None, derandomize=True)
@given(text=st.text(min_size=1, max_size=40))
def test_any_content_change_changes_the_hash(text):
    if text == unit()["source"]["text"]:
        return
    assert item_content_hash(unit(text=text)) != item_content_hash(unit())


def test_item_hash_tolerates_floats():
    assert item_content_hash(unit(score=0.5)) == item_content_hash(unit(score=Decimal("0.5")))


def test_pack_revision_binds_all_three_sources():
    base = pack_revision("p1", "f1", "i1")
    assert base != pack_revision("p2", "f1", "i1") != pack_revision("p1", "f2", "i1") != pack_revision("p1", "f1", "i2")
    assert base == pack_revision("p1", "f1", "i1")


def test_retry_cycle_is_three_retries_four_attempts():
    delays = (60.0, 300.0, 900.0)
    rng = random.Random(7)
    got = [retry_delay_seconds(n, delays, rng) for n in (1, 2, 3, 4)]
    assert got[3] is None and MAX_ATTEMPTS_PER_CYCLE == 4
    for value, base in zip(got[:3], delays):
        assert 0.8 * base <= value <= 1.2 * base
    replay = random.Random(7)
    assert [retry_delay_seconds(n, delays, replay) for n in (1, 2, 3)] == got[:3]  # deterministic


def test_retry_after_never_shortens_the_scheduled_delay():
    rng = random.Random(1)
    assert retry_delay_seconds(1, (60.0,), rng, retry_after=500.0) == 500.0
    assert retry_delay_seconds(1, (60.0,), random.Random(1), retry_after=1.0) >= 48.0
