"""6E-A spec §11.2: the pure, server-side result rule. The first matching
rule wins; SUBMISSION_FAILED only with proof that nothing was submitted."""
from __future__ import annotations

import dataclasses

from product.submit_certification import GREENHOUSE_SUBMIT
from product.submit_result import (
    NON_CLAIMS, ResultEvidence, build_submission_result, determine_submit_result, submission_result_hash,
)

E1 = 9201
GH = GREENHOUSE_SUBMIT


def ev(**overrides) -> ResultEvidence:
    base = dict(click_performed=True, egress_ever_installed=True, total_restored_verified=True,
                success_observed=False, failure_observed=False, content_changed=False,
                matched_rule_ids=(E1,), matched_rules_available=True, cause=None)
    base.update(overrides)
    return ResultEvidence(**base)


def decide(**overrides):
    return determine_submit_result(ev(**overrides), GH, E1)


def test_rule1_no_click_and_egress_never_installed_is_a_proven_failure():
    assert decide(click_performed=False, egress_ever_installed=False, cause="EGRESS_NOT_VERIFIED",
                  matched_rule_ids=()) == ("SUBMISSION_FAILED", True, "EGRESS_NOT_VERIFIED")


def test_rule1_no_click_egress_installed_restored_and_no_e1_match_is_proven():
    assert decide(click_performed=False, cause="SUBMIT_CONTROL_MISSING", matched_rule_ids=()) == \
        ("SUBMISSION_FAILED", True, "SUBMIT_CONTROL_MISSING")


def test_no_click_with_egress_installed_but_total_not_restored_is_ambiguous():
    assert decide(click_performed=False, total_restored_verified=False, matched_rule_ids=(),
                  cause="SUBMIT_CONTROL_MISSING")[0:2] == ("SUBMISSION_AMBIGUOUS", False)


def test_no_click_but_page_script_hit_e1_during_the_window_is_ambiguous():
    assert decide(click_performed=False, cause="SUBMIT_CONTROL_MISSING")[0:2] == ("SUBMISSION_AMBIGUOUS", False)


def test_rule2_content_change_beats_a_success_signal():
    assert decide(content_changed=True, success_observed=True) == ("SUBMISSION_AMBIGUOUS", False, "CONTENT_CHANGED")


def test_rule3_success_with_total_restored_is_confirmed():
    assert decide(success_observed=True) == ("CONFIRMED_SUCCESS", False, None)


def test_success_without_total_restored_is_ambiguous():
    assert decide(success_observed=True, total_restored_verified=False)[0] == "SUBMISSION_AMBIGUOUS"


def test_rule4_certified_failure_signal_is_a_proven_failure():
    assert decide(failure_observed=True) == ("SUBMISSION_FAILED", True, "EMPLOYER_VALIDATION_ERROR")


def test_failure_signal_without_certified_proof_is_ambiguous():
    cert = dataclasses.replace(GH, failure_signal_proves_not_submitted=False)
    assert determine_submit_result(ev(failure_observed=True), cert, E1)[0] == "SUBMISSION_AMBIGUOUS"


def test_rule5_no_e1_match_with_feedback_is_a_proven_failure():
    assert decide(matched_rule_ids=(9202,)) == ("SUBMISSION_FAILED", True, "NO_SUBMIT_REQUEST_LEFT")


def test_no_e1_match_without_feedback_is_ambiguous():
    assert decide(matched_rule_ids=(), matched_rules_available=False)[0] == "SUBMISSION_AMBIGUOUS"


def test_no_e1_match_but_total_not_restored_is_ambiguous():
    assert decide(matched_rule_ids=(), total_restored_verified=False)[0] == "SUBMISSION_AMBIGUOUS"


def test_timeout_with_nothing_observed_is_ambiguous_never_failed():  # Review Focus 3
    assert decide() == ("SUBMISSION_AMBIGUOUS", False, "NO_SIGNAL")


def test_unknown_click_after_restart_is_ambiguous():
    assert decide(click_performed="UNKNOWN", cause="EXECUTOR_RESTARTED") == \
        ("SUBMISSION_AMBIGUOUS", False, "EXECUTOR_RESTARTED")


def test_submission_result_record_carries_non_claims_and_no_cleartext():
    result = build_submission_result(
        attempt_id="att_1", authorization_id="hsa_1", grant_id="gr_1", review_hash="sha256:" + "9" * 64,
        fill_run_id="fr_1", state="CONFIRMED_SUCCESS", proven_not_submitted=False, reason=None,
        certification_id=GH.certification_id, events=[{"event": "CLICK_PERFORMED", "at": "2026-09-29T10:00:00+00:00"}],
        observations={"pre_submit_fingerprint": "sha256:" + "8" * 64}, matched_rule_ids=[E1],
        matched_rules_available=True, content_changed=False)
    assert result["schema"] == "submission-result" and result["schema_version"] == "v1"
    assert result["non_claims"] == NON_CLAIMS == {
        "employer_accepted": False, "employer_stored_application": "UNKNOWN",
        "content_equals_review_after_click": True, "delivered_to_recruiter": False}
    assert submission_result_hash(result) == submission_result_hash(dict(result))
    from webapp.persistence.fill import _check_no_cleartext
    _check_no_cleartext(result)


def test_content_change_is_reflected_in_the_non_claims():
    result = build_submission_result(
        attempt_id="a", authorization_id="h", grant_id="g", review_hash="r", fill_run_id="f",
        state="SUBMISSION_AMBIGUOUS", proven_not_submitted=False, reason="CONTENT_CHANGED",
        certification_id=GH.certification_id, events=[], observations={}, matched_rule_ids=[],
        matched_rules_available=True, content_changed=True)
    assert result["non_claims"]["content_equals_review_after_click"] is False
