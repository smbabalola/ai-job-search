"""Bundle 6E-A result determination and submission-result.v1 (spec §11, §15).
Pure. The server decides; the extension only reports evidence (E8).

SUBMISSION_FAILED is returned only with proof that nothing was submitted
(E9/J7). Proof is one of:
- no physical click, and either the egress allow rules were never installed
  (TOTAL untouched) or TOTAL was restored and verified with no match of the
  E1 submit rule while the egress existed (so no page script submitted
  either);
- the adapter's certified failure signal, when its certification says that
  signal proves nothing was stored;
- matched-rule feedback showing the E1 submit rule never matched, with TOTAL
  restored and verified.
Everything else that is not a certified success is SUBMISSION_AMBIGUOUS."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from product.autonomy_contract import canonical_hash
from product.submit_certification import SubmitCertification

SCHEMA = "submission-result"
SCHEMA_VERSION = "v1"
NON_CLAIMS = {"employer_accepted": False, "employer_stored_application": "UNKNOWN",
              "content_equals_review_after_click": True, "delivered_to_recruiter": False}


@dataclass(frozen=True)
class ResultEvidence:
    click_performed: bool | str  # True | False | "UNKNOWN"
    egress_ever_installed: bool
    total_restored_verified: bool
    success_observed: bool
    failure_observed: bool
    content_changed: bool
    matched_rule_ids: tuple[int, ...]
    matched_rules_available: bool
    cause: str | None


def determine_submit_result(e: ResultEvidence, cert: SubmitCertification,
                            e1_rule_id: int) -> tuple[str, bool, str | None]:
    """Returns (state, proven_not_submitted, reason). First match wins."""
    e1_never_matched = e.matched_rules_available and e1_rule_id not in e.matched_rule_ids
    if e.click_performed is False and (
            not e.egress_ever_installed or (e.total_restored_verified and e1_never_matched)):
        return "SUBMISSION_FAILED", True, e.cause
    if e.content_changed:
        return "SUBMISSION_AMBIGUOUS", False, "CONTENT_CHANGED"
    if e.success_observed and e.total_restored_verified:
        return "CONFIRMED_SUCCESS", False, None
    if e.failure_observed and cert.failure_signal_proves_not_submitted:
        return "SUBMISSION_FAILED", True, "EMPLOYER_VALIDATION_ERROR"
    if e1_never_matched and e.total_restored_verified:
        return "SUBMISSION_FAILED", True, "NO_SUBMIT_REQUEST_LEFT"
    return "SUBMISSION_AMBIGUOUS", False, e.cause or "NO_SIGNAL"


def build_submission_result(*, attempt_id: str, authorization_id: str, grant_id: str, review_hash: str,
                            fill_run_id: str, state: str, proven_not_submitted: bool, reason: str | None,
                            certification_id: str, events: list[dict[str, Any]], observations: dict[str, Any],
                            matched_rule_ids: list[int], matched_rules_available: bool,
                            content_changed: bool) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id, "authorization_id": authorization_id, "grant_id": grant_id,
        "review_hash": review_hash, "fill_run_id": fill_run_id,
        "state": state, "proven_not_submitted": proven_not_submitted, "reason": reason,
        "certification_id": certification_id, "events": events, "observations": observations,
        "matched_rule_ids": sorted(matched_rule_ids), "matched_rules_available": matched_rules_available,
        "non_claims": {**NON_CLAIMS, "content_equals_review_after_click": not content_changed},
    }


def submission_result_hash(result: dict[str, Any]) -> str:
    return canonical_hash(SCHEMA, SCHEMA_VERSION, result)
