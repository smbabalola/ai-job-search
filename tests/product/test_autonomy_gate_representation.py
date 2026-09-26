# tests/product/test_autonomy_gate_representation.py
from __future__ import annotations

from datetime import timedelta

import pytest

from product.autonomy_contract import (
    Capability, CompletionBlocker, EmployerKeyStrength, IdentityStrength, Reach,
    RepresentationRequirement, RequireUserItem, ResultKind, UNKNOWN,
)
from product.autonomy_gate import evaluate_authorization
from tests.product.autonomy_fixtures import NOW, answer, make_ctx

C, R = Capability, ResultKind


def req(key, subject, *, required=True, evidence=False, job_context=None, candidates=()):
    return RepresentationRequirement(key=key, subject=subject, required=required,
                                     evidence_available=evidence, job_context=job_context or {},
                                     candidates=tuple(candidates))


def run(*requirements, **overrides):
    return evaluate_authorization(make_ctx(requirements=tuple(requirements), **overrides))


def test_evidence_backed_fields_are_ready():
    assert run(req("email", None, evidence=True)).grantable
    assert run(req("rtw", "work_authorization.right_to_work", evidence=True)).grantable


def test_unclassified_required_field_requires_user_at_submit_only():
    d = run(req("q7", None))
    assert d.result is R.REQUIRE_USER and RequireUserItem("unclassified_field", "q7") in d.require_user_items
    d = run(req("q7", None), requested_stage=C.FILL)
    assert d.result is R.ALLOW and d.grantable
    assert run(req("q8", None, required=False)).grantable


def test_sensitive_required_pauses_optional_omitted():
    d = run(req("eeo", "demographic.eeo"))
    assert RequireUserItem("sensitive_field", "eeo") in d.require_user_items
    d = run(req("eeo", "demographic.eeo", required=False))
    assert d.grantable and any(r.code == "optional_omitted" for r in d.reasons)


def test_fresh_account_answer_is_submit_ready():
    # Ruling P: a profile-fact answer needs a checkable basis to be SUBMIT-ready
    # (the user-asserted case is pinned in the ruling-P tests below).
    fresh = answer("employment.notice_period", basis_kind="EVIDENCE", basis_at="sha256:a", basis_now="sha256:a")
    d = run(req("notice", "employment.notice_period", candidates=[fresh]))
    assert d.grantable


def test_expired_answer_caps_at_fill_without_deleting():
    old = answer("employment.notice_period", confirmed_at=NOW - timedelta(days=61))
    d = run(req("notice", "employment.notice_period", candidates=[old]))
    assert (d.result, d.effective_capability, d.grantable) == (R.ALLOW, C.FILL, False)
    assert any(r.code == "answer_not_submit_ready" and ("why", "expired") in r.params for r in d.reasons)
    assert run(req("notice", "employment.notice_period", candidates=[old]), requested_stage=C.FILL).grantable


def test_no_expiry_subject_but_basis_change_still_matters():
    ancient = answer("work_authorization.right_to_work", confirmed_at=NOW - timedelta(days=3650),
                     context={"country": "GB"}, basis_kind="EVIDENCE", basis_at="sha256:a", basis_now="sha256:a")
    job = {"country": "GB"}
    assert run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[ancient])).grantable
    changed = answer("work_authorization.right_to_work", context={"country": "GB"},
                     basis_kind="EVIDENCE", basis_at="sha256:a", basis_now="sha256:b")
    d = run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[changed]))
    assert d.effective_capability == C.FILL and not d.grantable
    gone = answer("work_authorization.right_to_work", context={"country": "GB"},
                  basis_kind="EVIDENCE", basis_at="sha256:a", basis_now=None)
    assert run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[gone])).effective_capability == C.FILL


def test_contradiction_requires_user_even_at_fill():
    bad = answer("employment.notice_period", contradicted=True)
    for stage in (C.FILL, C.SUBMIT):
        d = run(req("notice", "employment.notice_period", candidates=[bad]), requested_stage=stage)
        assert d.result is R.REQUIRE_USER
        assert RequireUserItem("contradicted_answer", "notice") in d.require_user_items


def test_reach_rules():
    other_ws = answer("mobility.relocation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_other",
                      context={"region": "ME"})
    d = run(req("reloc", "mobility.relocation", job_context={"region": "ME"}, candidates=[other_ws]))
    assert RequireUserItem("missing_answer", "reloc") in d.require_user_items
    same_ws = answer("mobility.relocation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_1", context={"region": "ME"})
    assert run(req("reloc", "mobility.relocation", job_context={"region": "ME"}, candidates=[same_ws])).grantable
    too_wide = answer("motivation.employer_specific", reach=Reach.ACCOUNT)
    d = run(req("why_us", "motivation.employer_specific", candidates=[too_wide]))
    assert RequireUserItem("missing_answer", "why_us") in d.require_user_items


def test_employer_bound_answer_and_generic_never_substitutes():
    wood = answer("motivation.employer_specific", reach=Reach.EMPLOYER, scope_id="name:acme")
    assert run(req("why_us", "motivation.employer_specific", candidates=[wood])).grantable
    generic = answer("motivation.role_type", reach=Reach.ACCOUNT)
    d = run(req("why_us", "motivation.employer_specific", candidates=[generic]))
    assert RequireUserItem("missing_answer", "why_us") in d.require_user_items
    # Unknown employer key: employer-bound answers unusable, and the per-employer
    # limit cannot be evaluated, so capability is capped at FILL; the question
    # cannot unlock SUBMIT, so it stays silent (relevance).
    d = run(req("why_us", "motivation.employer_specific", candidates=[wood]),
            employer_key_strength=EmployerKeyStrength.UNKNOWN)
    assert (d.result, d.effective_capability, d.grantable) == (R.ALLOW, C.FILL, False)


def test_context_keys():
    ctx = {"currency": "GBP", "region": "UK", "employment_type": "PERMANENT"}
    sal = answer("compensation.salary_expectation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_1", context=ctx)
    assert run(req("salary", "compensation.salary_expectation", job_context=ctx, candidates=[sal])).grantable
    unknown_region = {**ctx, "region": UNKNOWN}
    d = run(req("salary", "compensation.salary_expectation", job_context=unknown_region, candidates=[sal]))
    assert d.effective_capability == C.FILL
    usd = {**ctx, "currency": "USD"}
    d = run(req("salary", "compensation.salary_expectation", job_context=usd, candidates=[sal]))
    assert RequireUserItem("missing_answer", "salary") in d.require_user_items


def test_relevance_questions_that_cannot_unlock_anything_stay_silent():
    d = run(req("q7", None), workspace_ceiling=C.FILL)
    assert d.result is R.ALLOW and d.effective_capability == C.FILL and d.require_user_items == ()
    assert any(r.code == "unresolved_silent" for r in d.reasons)


def test_prepare_requests_ignore_fields():
    assert run(req("q7", None), requested_stage=C.PREPARE).grantable


# --- Fix round 1: gaps found in review ---------------------------------------


def test_optional_stale_answer_is_omitted_not_reduced():
    old = answer("employment.notice_period", confirmed_at=NOW - timedelta(days=61))
    d = run(req("notice", "employment.notice_period", required=False, candidates=[old]))
    assert (d.result, d.effective_capability, d.grantable) == (R.ALLOW, C.SUBMIT, True)
    assert any(r.code == "optional_omitted" and ("why", "expired") in r.params for r in d.reasons)

    ctx = {"currency": "GBP", "region": "UK", "employment_type": "PERMANENT"}
    sal = answer("compensation.salary_expectation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_1", context=ctx)
    unknown_region = {**ctx, "region": UNKNOWN}
    d2 = run(req("salary", "compensation.salary_expectation", required=False,
                 job_context=unknown_region, candidates=[sal]))
    assert d2.grantable
    assert any(r.code == "optional_omitted" and ("why", "context_unknown") in r.params for r in d2.reasons)


def test_employer_bound_answer_mismatched_scope_with_known_key():
    wood_other = answer("motivation.employer_specific", reach=Reach.EMPLOYER, scope_id="name:other")
    d = run(req("why_us", "motivation.employer_specific", candidates=[wood_other]))
    assert RequireUserItem("missing_answer", "why_us") in d.require_user_items


def test_basis_at_missing_but_current_present_is_basis_changed():
    cand = answer("work_authorization.right_to_work", context={"country": "GB"},
                  basis_kind="EVIDENCE", basis_at=None, basis_now="sha256:a")
    job = {"country": "GB"}
    d = run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[cand]))
    assert d.effective_capability == C.FILL
    assert any(r.code == "answer_not_submit_ready" and ("why", "basis_changed") in r.params for r in d.reasons)


def test_optional_field_with_no_candidates_is_omitted_no_answer():
    d = run(req("notice", "employment.notice_period", required=False, candidates=()))
    assert d.grantable
    assert any(r.code == "optional_omitted" and ("why", "no_answer") in r.params for r in d.reasons)


def test_contradiction_silenced_by_relevance():
    bad = answer("employment.notice_period", contradicted=True)
    d = run(req("notice", "employment.notice_period", candidates=[bad]),
            identity_strength=IdentityStrength.WEAK)
    assert RequireUserItem("contradicted_answer", "notice") not in d.require_user_items
    assert any(r.code == "unresolved_silent" and ("kind", "contradicted_answer") in r.params
               and ("ref", "notice") in r.params for r in d.reasons)


def test_basis_changed_and_basis_gone_carry_basis_changed_reason():
    job = {"country": "GB"}
    changed = answer("work_authorization.right_to_work", context={"country": "GB"},
                     basis_kind="EVIDENCE", basis_at="sha256:a", basis_now="sha256:b")
    d = run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[changed]))
    assert any(r.code == "answer_not_submit_ready" and ("why", "basis_changed") in r.params for r in d.reasons)

    gone = answer("work_authorization.right_to_work", context={"country": "GB"},
                  basis_kind="EVIDENCE", basis_at="sha256:a", basis_now=None)
    d2 = run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[gone]))
    assert any(r.code == "answer_not_submit_ready" and ("why", "basis_changed") in r.params for r in d2.reasons)


# --- Follow-up ruling K: a required field with no permitted source caps at
# FILL exactly as an expired required answer does (spec §9.3 step 3), while
# still following the existing stage/relevance rules for whether its item
# is surfaced or silent. Optional (required=False) fields stay non-blocking.


def test_required_missing_answer_caps_at_fill_when_submit_requested():
    d = run(req("notice", "employment.notice_period", candidates=()))
    assert (d.result, d.effective_capability) == (R.REQUIRE_USER, C.FILL)
    assert any(r.code == "required_field_unresolved" and ("field", "notice") in r.params
               and ("kind", "missing_answer") in r.params for r in d.reasons)


def test_required_contradicted_answer_caps_at_fill_when_submit_requested():
    bad = answer("employment.notice_period", contradicted=True)
    d = run(req("notice", "employment.notice_period", candidates=[bad]))
    assert (d.result, d.effective_capability) == (R.REQUIRE_USER, C.FILL)
    assert any(r.code == "required_field_unresolved" and ("field", "notice") in r.params
               and ("kind", "contradicted_answer") in r.params for r in d.reasons)


def test_required_unclassified_and_sensitive_cap_at_fill_when_submit_requested():
    d = run(req("q7", None))
    assert d.result is R.REQUIRE_USER and d.effective_capability == C.FILL
    assert any(r.code == "required_field_unresolved" and ("kind", "unclassified_field") in r.params for r in d.reasons)
    d = run(req("eeo", "demographic.eeo"))
    assert d.result is R.REQUIRE_USER and d.effective_capability == C.FILL
    assert any(r.code == "required_field_unresolved" and ("kind", "sensitive_field") in r.params for r in d.reasons)


def test_optional_missing_and_contradicted_fields_do_not_cap():
    d = run(req("notice", "employment.notice_period", required=False, candidates=()))
    assert d.effective_capability == C.SUBMIT and d.grantable
    assert not any(r.code == "required_field_unresolved" for r in d.reasons)
    bad = answer("employment.notice_period", contradicted=True)
    d2 = run(req("notice", "employment.notice_period", required=False, candidates=[bad]))
    assert d2.effective_capability == C.SUBMIT
    assert not any(r.code == "required_field_unresolved" for r in d2.reasons)


def test_missing_required_field_silent_when_structural_cap_already_fill_still_caps():
    """The cap is unconditional (an actionable reduction applied after
    relevance is judged), so it still applies even when the item itself is
    silenced by relevance -- with no *observable* effect here, since the
    structural cap (from workspace_ceiling) was already FILL."""
    d = run(req("q7", None), workspace_ceiling=C.FILL)
    assert d.result is R.ALLOW and d.effective_capability == C.FILL
    assert RequireUserItem("unclassified_field", "q7") not in d.require_user_items
    assert any(r.code == "unresolved_silent" and ("kind", "unclassified_field") in r.params for r in d.reasons)
    assert any(r.code == "required_field_unresolved" for r in d.reasons)


def test_required_field_unresolved_cap_applies_at_fill_too_not_only_submit():
    """Corrected per the coordinator's fix round: effective_capability always
    states the highest level that can actually proceed *now*, for whatever
    stage was requested (spec §9.3 step 3 and the FILL-is-authority-not-
    completeness note in §9.5) -- so the cap applies at a FILL request too,
    not only at SUBMIT (superseding the SUBMIT-only gating this test used to
    assert, which was itself a mistaken simplification later corrected).
    At PREPARE the cap still never applies, because _apply_requirements
    itself never runs below FILL."""
    bad = answer("employment.notice_period", contradicted=True)
    d = run(req("notice", "employment.notice_period", candidates=[bad]), requested_stage=C.FILL)
    assert d.effective_capability == C.FILL
    assert any(r.code == "required_field_unresolved" and ("kind", "contradicted_answer") in r.params
               for r in d.reasons)
    d = run(req("q7", None), requested_stage=C.PREPARE)
    assert not any(r.code == "required_field_unresolved" for r in d.reasons)


def test_required_field_unresolved_cap_never_raises_capability():
    optional_cap = run(req("notice", "employment.notice_period", required=False, candidates=())).effective_capability
    required_cap = run(req("notice", "employment.notice_period", required=True, candidates=())).effective_capability
    assert required_cap <= optional_cap


# --- Fix round for the K/L follow-up: the SUBMIT-only gate above was itself
# a mistaken simplification (spec §9.3 step 3: effective_capability ALWAYS
# states the highest level that can actually proceed now, for whatever
# stage was requested; a worse answer state can never record a higher
# capability). The cap now applies whenever requirements are evaluated at
# all (requested_stage >= FILL), not only at SUBMIT.


def test_missing_answer_required_field_silent_when_structural_cap_already_fill_still_caps():
    """A correctly named duplicate of
    test_missing_required_field_silent_when_structural_cap_already_fill_still_caps:
    that test's name promises an actual missing *answer* but its req("q7",
    None) is unclassified (subject=None), not missing_answer (a classified
    subject with no candidates). This exercises the real missing_answer
    case instead; the original test is left as-is."""
    d = run(req("notice", "employment.notice_period", candidates=()), workspace_ceiling=C.FILL)
    assert d.result is R.ALLOW and d.effective_capability == C.FILL
    assert RequireUserItem("missing_answer", "notice") not in d.require_user_items
    assert any(r.code == "unresolved_silent" and ("kind", "missing_answer") in r.params for r in d.reasons)
    assert any(r.code == "required_field_unresolved" and ("kind", "missing_answer") in r.params for r in d.reasons)


@pytest.mark.parametrize("stage", [C.FILL, C.SUBMIT])
def test_required_field_answer_state_chain_never_increases_capability(stage):
    """Walk a required field from a fresh answer through progressively worse
    states (fresh -> expired -> missing -> contradicted -> unclassified ->
    sensitive); effective_capability must never rise from one state to the
    next, at EITHER requested stage, since the fix round corrected the
    required_field_unresolved cap to apply at FILL too, not only SUBMIT.

    grantable is checked the same way (pairwise, never rises) at SUBMIT,
    where every one of these item kinds surfaces. At FILL it is checked
    per-state instead of pairwise: contradicted_answer items uniquely
    surface at FILL too (pre-existing, spec-mandated behaviour unrelated to
    rulings K/L -- see test_contradiction_requires_user_even_at_fill), while
    missing_answer/unclassified_field/sensitive_field items are SUBMIT-only
    and so are silenced by relevance at a FILL request. A strict pairwise
    "never increases" over this fixed chain ordering would therefore
    wrongly flag the step immediately after "contradicted" as a
    monotonicity violation, when the actual ruling-K property --
    effective_capability itself never rising -- still holds throughout."""
    fresh = answer("employment.notice_period", confirmed_at=NOW)
    expired = answer("employment.notice_period", confirmed_at=NOW - timedelta(days=61))
    contradicted = answer("employment.notice_period", contradicted=True)
    chain = [
        ("fresh", req("notice", "employment.notice_period", candidates=[fresh])),
        ("expired", req("notice", "employment.notice_period", candidates=[expired])),
        ("missing", req("notice", "employment.notice_period", candidates=())),
        ("contradicted", req("notice", "employment.notice_period", candidates=[contradicted])),
        ("unclassified", req("q7", None)),
        ("sensitive", req("eeo", "demographic.eeo")),
    ]
    decisions = [(name, run(r, requested_stage=stage)) for name, r in chain]
    for (prev_name, prev), (cur_name, cur) in zip(decisions, decisions[1:]):
        assert cur.effective_capability <= prev.effective_capability, (
            f"{cur_name} capability rose above {prev_name} at requested_stage={stage.name}")
    by_name = dict(decisions)
    if stage == C.SUBMIT:
        for (prev_name, prev), (cur_name, cur) in zip(decisions, decisions[1:]):
            assert not (cur.grantable and not prev.grantable), (
                f"{cur_name} grantable rose above {prev_name} at requested_stage=SUBMIT")
    else:
        assert by_name["fresh"].grantable
        assert by_name["expired"].grantable
        assert by_name["missing"].grantable
        assert not by_name["contradicted"].grantable
        assert by_name["unclassified"].grantable
        assert by_name["sensitive"].grantable
        # The corrected behaviour this fix round exists for: at a FILL
        # request, a missing or contradicted required answer now caps at
        # FILL too (it used to wrongly stay at the SUBMIT-level ceiling,
        # since the superseded code only applied the cap at requested_stage
        # == SUBMIT).
        assert by_name["missing"].effective_capability == C.FILL
        assert by_name["contradicted"].effective_capability == C.FILL


# --- Follow-up ruling M: a NON-required field's contradicted answer is
# omitted (optional_omitted why=contradicted), not a question -- it raises no
# RequireUserItem and applies no cap. Required fields are unchanged (spec §7.5).


def test_optional_contradicted_answer_is_omitted_not_a_question():
    bad = answer("employment.notice_period", contradicted=True)
    for stage in (C.FILL, C.SUBMIT):
        baseline = run(requested_stage=stage)
        d = run(req("notice", "employment.notice_period", required=False, candidates=[bad]),
                requested_stage=stage)
        assert d.effective_capability == baseline.effective_capability
        assert d.grantable is baseline.grantable is True
        assert RequireUserItem("contradicted_answer", "notice") not in d.require_user_items
        assert any(r.code == "optional_omitted" and ("why", "contradicted") in r.params for r in d.reasons)
        assert not any(r.code == "unresolved_silent" and ("kind", "contradicted_answer") in r.params
                       for r in d.reasons)


def test_required_contradicted_answer_unchanged_by_ruling_m():
    """Ruling M only changes optional (non-required) fields; a required
    field's contradicted answer still raises its item at FILL and SUBMIT and
    still caps at FILL (unchanged from test_contradiction_requires_user_even_at_fill
    and the ruling-K tests above)."""
    bad = answer("employment.notice_period", contradicted=True)
    for stage in (C.FILL, C.SUBMIT):
        d = run(req("notice", "employment.notice_period", required=True, candidates=[bad]),
                requested_stage=stage)
        assert d.result is R.REQUIRE_USER and d.effective_capability == C.FILL
        assert RequireUserItem("contradicted_answer", "notice") in d.require_user_items


# --- Follow-up ruling O: completion_blockers (spec §9.5) --------------------
# A machine-readable entry for every REQUIRED field with no fillable
# permitted value (missing_answer, contradicted_answer, unclassified_field,
# sensitive_field), recorded whenever requirements are evaluated (FILL and
# SUBMIT), regardless of relevance, and never affecting result/effective_
# capability/grantable. Expired/stale/context-unknown required answers are
# fillable (just not SUBMIT-ready), so they are NOT completion blockers;
# optional fields never are.


def test_required_missing_answer_records_completion_blocker_at_fill_and_submit():
    blocker = CompletionBlocker("notice", "employment.notice_period", "missing_answer", C.SUBMIT)
    d = run(req("notice", "employment.notice_period", candidates=()), requested_stage=C.FILL)
    assert d.result is R.ALLOW and d.effective_capability == C.FILL and d.grantable
    assert blocker in d.completion_blockers

    d2 = run(req("notice", "employment.notice_period", candidates=()), requested_stage=C.SUBMIT)
    assert d2.result is R.REQUIRE_USER and d2.effective_capability == C.FILL and not d2.grantable
    assert blocker in d2.completion_blockers


def test_required_contradicted_answer_records_completion_blocker():
    bad = answer("employment.notice_period", contradicted=True)
    blocker = CompletionBlocker("notice", "employment.notice_period", "contradicted_answer", C.SUBMIT)
    for stage in (C.FILL, C.SUBMIT):
        d = run(req("notice", "employment.notice_period", candidates=[bad]), requested_stage=stage)
        assert blocker in d.completion_blockers


def test_required_unclassified_and_sensitive_record_completion_blockers():
    d = run(req("q7", None), requested_stage=C.SUBMIT)
    assert CompletionBlocker("q7", None, "unclassified_field", C.SUBMIT) in d.completion_blockers
    d = run(req("eeo", "demographic.eeo"), requested_stage=C.SUBMIT)
    assert CompletionBlocker("eeo", "demographic.eeo", "sensitive_field", C.SUBMIT) in d.completion_blockers


def test_required_unclassified_and_sensitive_record_completion_blockers_at_fill_too():
    """Test gap: the SUBMIT-only test above didn't cover FILL. unclassified_field
    and sensitive_field items never surface at FILL (raise_at_fill=False), so
    result stays ALLOW -- but the completion blocker is still recorded,
    exactly as it is for missing_answer (spec §9.5: recorded whenever
    requirements are evaluated, FILL and SUBMIT, regardless of relevance)."""
    d = run(req("q7", None), requested_stage=C.FILL)
    assert d.result is R.ALLOW and d.effective_capability == C.FILL and d.grantable
    assert CompletionBlocker("q7", None, "unclassified_field", C.SUBMIT) in d.completion_blockers

    d2 = run(req("eeo", "demographic.eeo"), requested_stage=C.FILL)
    assert d2.result is R.ALLOW and d2.effective_capability == C.FILL and d2.grantable
    assert CompletionBlocker("eeo", "demographic.eeo", "sensitive_field", C.SUBMIT) in d2.completion_blockers


def test_expired_required_answer_is_not_a_completion_blocker():
    old = answer("employment.notice_period", confirmed_at=NOW - timedelta(days=61))
    d = run(req("notice", "employment.notice_period", candidates=[old]), requested_stage=C.SUBMIT)
    assert d.effective_capability == C.FILL  # still capped (not submit-ready)...
    assert d.completion_blockers == ()  # ...but not a completion blocker: the field is fillable


def test_stale_by_basis_and_context_unknown_required_answers_are_not_completion_blockers():
    """Test gap: basis_changed and context_unknown required answers go
    through the same `reductions` path as `expired` (the field is fillable,
    just not SUBMIT-ready), so neither is a completion blocker."""
    job = {"country": "GB"}
    basis_changed = answer("work_authorization.right_to_work", context={"country": "GB"},
                            basis_kind="EVIDENCE", basis_at="sha256:a", basis_now="sha256:b")
    d = run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[basis_changed]),
            requested_stage=C.SUBMIT)
    assert d.effective_capability == C.FILL
    assert d.completion_blockers == ()

    ctx = {"currency": "GBP", "region": "UK", "employment_type": "PERMANENT"}
    sal = answer("compensation.salary_expectation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_1", context=ctx)
    unknown_region = {**ctx, "region": UNKNOWN}
    d2 = run(req("salary", "compensation.salary_expectation", job_context=unknown_region, candidates=[sal]),
             requested_stage=C.SUBMIT)
    assert d2.effective_capability == C.FILL
    assert d2.completion_blockers == ()


def test_optional_field_never_records_a_completion_blocker():
    contradicted = answer("employment.notice_period", contradicted=True)
    for r in (
        req("notice", "employment.notice_period", required=False, candidates=()),
        req("notice", "employment.notice_period", required=False, candidates=[contradicted]),
        req("q7", None, required=False),
        req("eeo", "demographic.eeo", required=False),
    ):
        d = run(r, requested_stage=C.SUBMIT)
        assert d.completion_blockers == ()


def test_prepare_stage_records_no_completion_blockers():
    d = run(req("notice", "employment.notice_period", candidates=()), requested_stage=C.PREPARE)
    assert d.completion_blockers == ()


def test_completion_blockers_recorded_even_when_silenced_by_relevance():
    """The structural cap (from workspace_ceiling) is already FILL, below the
    SUBMIT request, so relevance silences the missing_answer item -- but the
    completion blocker is still recorded, unlike the REQUIRE_USER item."""
    d = run(req("notice", "employment.notice_period", candidates=()),
            requested_stage=C.SUBMIT, workspace_ceiling=C.FILL)
    assert d.result is R.ALLOW and d.effective_capability == C.FILL
    assert RequireUserItem("missing_answer", "notice") not in d.require_user_items
    assert any(r.code == "unresolved_silent" and ("kind", "missing_answer") in r.params for r in d.reasons)
    assert CompletionBlocker("notice", "employment.notice_period", "missing_answer", C.SUBMIT) in d.completion_blockers


def test_completion_blockers_identical_whether_item_surfaced_or_silenced():
    """completion_blockers never change result/effective_capability/grantable
    (spec §9.5): the SAME blocker is recorded whether its item is silenced
    (a FILL request never surfaces missing_answer) or surfaced (a SUBMIT
    request does) -- only `result` and `grantable` differ, driven by the
    pre-existing relevance/stage logic, not by the blocker's presence."""
    blocker = CompletionBlocker("notice", "employment.notice_period", "missing_answer", C.SUBMIT)
    silent = run(req("notice", "employment.notice_period", candidates=()), requested_stage=C.FILL)
    surfaced = run(req("notice", "employment.notice_period", candidates=()), requested_stage=C.SUBMIT)
    assert silent.result is R.ALLOW and surfaced.result is R.REQUIRE_USER
    assert silent.grantable and not surfaced.grantable
    assert silent.completion_blockers == surfaced.completion_blockers == (blocker,)
    assert silent.effective_capability == surfaced.effective_capability == C.FILL


def test_completion_blockers_ordering_is_deterministic_and_sorted():
    """Test gap: several simultaneous completion blockers are sorted
    deterministically (spec §9.5), independent of the order the underlying
    requirements were supplied in."""
    bad = answer("employment.notice_period", contradicted=True)
    missing = req("aaa_notice", "employment.notice_period", candidates=())
    unclassified = req("zzz_q7", None)
    sensitive = req("mmm_eeo", "demographic.eeo")
    contradicted = req("bbb_notice2", "employment.notice_period", candidates=[bad])
    d1 = run(missing, unclassified, sensitive, contradicted, requested_stage=C.SUBMIT)
    d2 = run(contradicted, sensitive, unclassified, missing, requested_stage=C.SUBMIT)
    assert len(d1.completion_blockers) == 4
    assert d1.completion_blockers == tuple(sorted(d1.completion_blockers))
    assert d1.completion_blockers == d2.completion_blockers


def test_optional_field_completion_blockers_match_no_field_baseline():
    baseline = run(requested_stage=C.SUBMIT)
    with_optional = run(req("notice", "employment.notice_period", required=False, candidates=()),
                         requested_stage=C.SUBMIT)
    assert with_optional.completion_blockers == baseline.completion_blockers == ()
    assert with_optional.result == baseline.result
    assert with_optional.effective_capability == baseline.effective_capability
    assert with_optional.grantable == baseline.grantable


def test_optional_field_answer_state_chain_never_lowers_below_no_field_baseline():
    """The mirror of the required-field chain: an optional field's worst
    answer state must never cap capability below the baseline a context
    with no such field at all would get."""
    baseline = evaluate_authorization(make_ctx()).effective_capability
    fresh = answer("employment.notice_period", confirmed_at=NOW)
    expired = answer("employment.notice_period", confirmed_at=NOW - timedelta(days=61))
    contradicted = answer("employment.notice_period", contradicted=True)
    optional_chain = [
        req("notice", "employment.notice_period", required=False, candidates=[fresh]),
        req("notice", "employment.notice_period", required=False, candidates=[expired]),
        req("notice", "employment.notice_period", required=False, candidates=()),
        req("notice", "employment.notice_period", required=False, candidates=[contradicted]),
        req("q7", None, required=False),
        req("eeo", "demographic.eeo", required=False),
    ]
    for r in optional_chain:
        assert run(r).effective_capability == baseline


def test_optional_field_answer_state_chain_grantable_parity_with_no_field_baseline():
    """New test (the existing chain test above deliberately checks only
    effective_capability, not grantable -- see its docstring history). After
    ruling M, every optional worst-tier state -- contradicted included -- no
    longer raises any item, so grantable now matches the no-field baseline
    too, not only effective_capability."""
    baseline = evaluate_authorization(make_ctx())
    fresh = answer("employment.notice_period", confirmed_at=NOW)
    expired = answer("employment.notice_period", confirmed_at=NOW - timedelta(days=61))
    contradicted = answer("employment.notice_period", contradicted=True)
    optional_chain = [
        req("notice", "employment.notice_period", required=False, candidates=[fresh]),
        req("notice", "employment.notice_period", required=False, candidates=[expired]),
        req("notice", "employment.notice_period", required=False, candidates=()),
        req("notice", "employment.notice_period", required=False, candidates=[contradicted]),
        req("q7", None, required=False),
        req("eeo", "demographic.eeo", required=False),
    ]
    for r in optional_chain:
        d = run(r)
        assert d.effective_capability == baseline.effective_capability
        assert d.grantable == baseline.grantable


# ---- ruling P: profile facts need a checkable basis for unattended SUBMIT ----

def _why(d, code="answer_not_submit_ready"):
    return {dict(r.params).get("why") for r in d.reasons if r.code == code}


def test_user_asserted_profile_fact_is_not_submit_ready_but_still_fillable():
    rtw = answer("work_authorization.right_to_work", context={"country": "GB"})
    job = {"country": "GB"}
    d = run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[rtw]))
    assert (d.result, d.effective_capability, d.grantable) == (R.ALLOW, C.FILL, False)
    assert "profile_basis_unverifiable" in _why(d)
    assert d.completion_blockers == () and d.require_user_items == ()  # fillable, no question
    d = run(req("rtw", "work_authorization.right_to_work", job_context=job, candidates=[rtw]),
            requested_stage=C.FILL)
    assert d.result is R.ALLOW and d.grantable


def test_evidence_based_profile_fact_is_checked_by_basis_hash():
    ok = answer("work_authorization.right_to_work", context={"country": "GB"},
                basis_kind="EVIDENCE", basis_at="sha256:a", basis_now="sha256:a")
    assert run(req("rtw", "work_authorization.right_to_work", job_context={"country": "GB"},
                   candidates=[ok])).grantable


def test_user_asserted_non_profile_subjects_unaffected():
    assert run(req("why", "motivation.role_type", candidates=[answer("motivation.role_type")])).grantable
    salary = answer("compensation.salary_expectation", reach=Reach.SEARCH_WORKSPACE, scope_id="sw_1",
                    context={"currency": "GBP", "region": "UK", "employment_type": "PERMANENT"})
    assert run(req("sal", "compensation.salary_expectation", candidates=[salary],
                   job_context={"currency": "GBP", "region": "UK", "employment_type": "PERMANENT"})).grantable


def test_optional_user_asserted_profile_fact_is_omitted_without_cap():
    d = run(req("lic", "licence.driving", required=False, candidates=[answer("licence.driving")]))
    assert d.grantable and d.effective_capability == C.SUBMIT
    assert "profile_basis_unverifiable" in _why(d, "optional_omitted")
