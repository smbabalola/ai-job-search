# tests/product/test_autonomy_gate_representation.py
from __future__ import annotations

from datetime import timedelta

from product.autonomy_contract import (
    Capability, EmployerKeyStrength, IdentityStrength, Reach, RepresentationRequirement,
    RequireUserItem, ResultKind, UNKNOWN,
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
    d = run(req("notice", "employment.notice_period", candidates=[answer("employment.notice_period")]))
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
