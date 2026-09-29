"""Bundle 6E-A human-authorized SUBMIT (spec §8, §10, §11, §13).

The single human act -- "Submit application" on the Submit Review page --
authorizes exactly one reviewed application version (review_hash). It is
the only path to a SUBMIT grant (request_human_submit_grant) and, later, to
the 6B pre-click commit under HUMAN_SUBMIT authority. The autonomous
entry points (autonomy.request_grant(SUBMIT), autonomy.pre_click_commit)
keep refusing: 6E-B does not exist.

Every function owns one BEGIN IMMEDIATE transaction; a refusal raises
SubmitRefused (routes: 409 with the reason)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from product.autonomy_contract import AuthorityKind, parse_utc
from product.submit_certification import submit_certified
from product.submit_constants import MATCHED_ALLOW_RULES_REPORTED
from product.submit_result import (
    ResultEvidence, build_submission_result, determine_submit_result, submission_result_hash,
)
from webapp.persistence.autonomy_ledger import link_intent_workflow_event
from webapp.persistence.migrations import SUBMIT_EVENTS
from webapp.persistence.workflow import record_status_change
from webapp.config import Settings
from webapp.persistence import fill as f
from webapp.persistence import submit as sp
from webapp.persistence.autonomy_ledger import append_attempt_event, attempt_state, get_grant, revoke_grant
from webapp.services import autonomy as a
from webapp.services.autonomy import AutonomyPaused, request_human_submit_grant
from webapp.services.autonomy_controls import kill_switch_state, run_immediate, sentinel_present
from webapp.services.fill_runs import target_observation
from webapp.services.submit_review import (
    SubmitRefused, assemble, gate_reasons, human_manifest, record_submit_observation,
)

__all__ = ["SubmitRefused", "authorize", "cancel_authorization", "cancel_attempt", "human_pre_click_commit",
           "human_record_click_dispatched", "record_submit_event", "report_result", "resolve",
           "submission_status"]

E1_RULE_ID = 9201  # SUBMIT_ALLOW_RULE_BASE + index of E1_SUBMIT in the certified egress

_CONTEXT_KEYS = ("executor_instance_id", "browser_session_id", "execution_tab_id")


def refusal_reason(decision) -> str:
    reasons = gate_reasons(decision)
    if "human_submit_disabled" in reasons:
        return "human_submit_disabled"
    return reasons[0]


def authorize(conn, *, settings: Settings, account_id: str, application_workspace_id: str, review_hash: str,
              actor: str, now: datetime) -> dict[str, Any]:
    """Spec §8.3. One transaction: rebuild the snapshot from the latest REVIEW
    observation (must equal review_hash), evaluate HUMAN_SUBMIT, insert the
    authorization and issue its SUBMIT grant. A gate refusal still commits
    the audited decision row, then raises."""
    def work() -> dict[str, Any]:
        assembled = assemble(conn, settings=settings, account_id=account_id,
                             application_workspace_id=application_workspace_id, now=now)
        if assembled.review_hash != review_hash:
            raise SubmitRefused("stale_review")
        authorization_id = sp.new_authorization_id()
        outcome = request_human_submit_grant(
            conn, settings=settings, account_id=account_id, application_workspace_id=application_workspace_id,
            authorization_id=authorization_id, review_hash=review_hash,
            fill_manifest=human_manifest(conn, assembled.plan, assembled.approval),
            observation=target_observation(assembled.plan), submit_origin=assembled.inputs.origin, now=now)
        if outcome.grant is None:
            return {"refused": refusal_reason(outcome.decision)}
        sp.insert_authorization(conn, id=authorization_id, account_id=account_id,
                                application_workspace_id=application_workspace_id,
                                fill_run_id=assembled.run["id"], review_hash=review_hash, review=assembled.snapshot,
                                grant_id=outcome.grant["id"], actor=actor, now=now)
        return {"authorization_id": authorization_id, "grant_id": outcome.grant["id"],
                "expires_at": outcome.grant["expires_at"]}
    try:
        out = run_immediate(conn, work)
    except AutonomyPaused as exc:
        raise SubmitRefused("paused") from exc
    if "refused" in out:
        raise SubmitRefused(out["refused"])
    return out


def _owned_authorization(conn, account_id: str, authorization_id: str) -> dict[str, Any]:
    auth = sp.get_authorization(conn, authorization_id)
    if auth is None or auth["account_id"] != account_id:
        raise SubmitRefused("not_found")
    return auth


def cancel_authorization(conn, *, account_id: str, authorization_id: str, actor: str,
                         now: datetime) -> dict[str, Any]:
    """Spec §8.3/E14: before the grant is consumed, revoke it. Afterwards
    only an attempt still AUTHORIZED (not dispatched) can be cancelled."""
    def work() -> dict[str, Any]:
        auth = _owned_authorization(conn, account_id, authorization_id)
        grant = get_grant(conn, auth["grant_id"])
        if grant["status"] == "ISSUED":
            revoke_grant(conn, grant_id=grant["id"], reason="user_cancelled", now=now)
            sp.append_submit_event(conn, authorization_id=authorization_id, attempt_id=None,
                                   event="CANCELLED_BEFORE_DISPATCH", detail={"actor": actor, "stage": "GRANT"},
                                   now=now)
            return {"cancelled": True, "stage": "GRANT"}
        attempt = _attempt_for_grant(conn, grant["id"])
        if attempt is not None and attempt_state(conn, attempt["id"]) == "AUTHORIZED":
            _cancel_attempt_in_transaction(conn, attempt, authorization_id, actor, now)
            return {"cancelled": True, "stage": "ATTEMPT", "attempt_id": attempt["id"]}
        raise SubmitRefused("not_cancellable")
    return run_immediate(conn, work)


# ---- pre-click, dispatch, cancel (spec §8.3, §10 steps 3-5, E14, E15) -----------------------

def _attempt_for_grant(conn, grant_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM submission_attempts WHERE grant_id = ?", (grant_id,)).fetchone()
    return dict(row) if row else None


def _authorization_for_attempt(conn, attempt: dict[str, Any]) -> dict[str, Any]:
    auth = sp.authorization_for_grant(conn, attempt["grant_id"])
    if auth is None:
        raise SubmitRefused("not_authorized")
    return auth


def _cancel_attempt_in_transaction(conn, attempt: dict[str, Any], authorization_id: str, actor: str,
                                   now: datetime) -> None:
    append_attempt_event(conn, attempt_id=attempt["id"], state="EXPIRED_UNCLICKED", source="USER",
                         evidence={"cancelled": True, "actor": actor}, now=now)
    a._release(conn, attempt, now)
    sp.append_submit_event(conn, authorization_id=authorization_id, attempt_id=attempt["id"],
                           event="CANCELLED_BEFORE_DISPATCH", detail={"actor": actor, "stage": "ATTEMPT"}, now=now)


def cancel_attempt(conn, *, account_id: str, attempt_id: str, actor: str, now: datetime) -> dict[str, Any]:
    """Only an attempt still AUTHORIZED (CLICK_DISPATCHED not recorded)."""
    def work() -> dict[str, Any]:
        attempt = a._attempt(conn, attempt_id)
        auth = _authorization_for_attempt(conn, attempt)
        if auth["account_id"] != account_id:
            raise SubmitRefused("not_found")
        if attempt_state(conn, attempt_id) != "AUTHORIZED":
            raise SubmitRefused("already_dispatched")
        _cancel_attempt_in_transaction(conn, attempt, auth["id"], actor, now)
        return {"cancelled": True, "attempt_id": attempt_id}
    return run_immediate(conn, work)


def human_pre_click_commit(conn, *, settings: Settings, run_id: str, grant_id: str, observation: dict[str, Any],
                           verification: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Spec §8.3. The PRE_SUBMIT observation is stored first (evidence even
    when refused). Then one transaction proves, in order: the authorization
    and its run; the live context (tab, browser session, executor); no
    visible challenge; every §7.2 precondition on PRE_SUBMIT; the
    extension's verification against the authorized snapshot; the exact
    review hash; the grant still consumable; and finally the 6B pre-click
    core under HUMAN_SUBMIT. Every refusal revokes the grant, records
    PRE_CLICK_REFUSED (or CHALLENGE_BEFORE_SUBMIT) and raises after commit."""
    auth = sp.authorization_for_grant(conn, grant_id)
    if auth is None or auth["fill_run_id"] != run_id:
        raise SubmitRefused("not_authorized")
    stored = record_submit_observation(conn, settings=settings, run_id=run_id, phase="PRE_SUBMIT", attempt_id=None,
                                       observation=observation, now=now)

    def refuse(reason: str, event: str = "PRE_CLICK_REFUSED") -> dict[str, Any]:
        revoke_grant(conn, grant_id=grant_id, reason=f"pre_click:{reason}", now=now)
        sp.append_submit_event(conn, authorization_id=auth["id"], attempt_id=None, event=event,
                               detail={"reason": reason, "observation_id": stored["id"]}, now=now)
        return {"refused": reason}

    def work() -> dict[str, Any]:
        run = f.get_run(conn, run_id)
        if any(verification.get(k) != run[k] for k in _CONTEXT_KEYS):
            return refuse("context_mismatch")
        if verification.get("challenge_visible"):
            return refuse("challenge_before_submit", "CHALLENGE_BEFORE_SUBMIT")
        try:
            assembled = assemble(conn, settings=settings, account_id=auth["account_id"],
                                 application_workspace_id=auth["application_workspace_id"], now=now,
                                 observation_row=stored)
        except SubmitRefused as refusal:
            return refuse(refusal.reason)
        snap = assembled.snapshot
        expected = {"canonical_url": snap["target"]["canonical_url"],
                    "observation_fingerprint": snap["observation"]["observation_fingerprint"],
                    "submit_control_fingerprint": snap["submit_control"]["control_fingerprint"],
                    "ruleset_hash": snap["fill"]["ruleset_hash_total"]}
        if any(verification.get(k) != v for k, v in expected.items()):
            return refuse("verification_mismatch")
        if assembled.review_hash != auth["review_hash"]:
            return refuse("review_changed")
        grant = get_grant(conn, grant_id)
        if grant["status"] != "ISSUED" or parse_utc(grant["expires_at"]) <= now:
            sp.append_submit_event(conn, authorization_id=auth["id"], attempt_id=None, event="PRE_CLICK_REFUSED",
                                   detail={"reason": "grant_not_consumable"}, now=now)
            return {"refused": "grant_not_consumable"}
        result = a._pre_click_commit_core(
            conn, settings=settings, grant_id=grant_id, verification={}, now=now,
            observation=target_observation(assembled.plan), authority=AuthorityKind.HUMAN_SUBMIT,
            intent_source="HUMAN_AUTHORIZED", submit_origin=assembled.inputs.origin, in_transaction=True)
        if not result.authorized:
            sp.append_submit_event(conn, authorization_id=auth["id"], attempt_id=None, event="PRE_CLICK_REFUSED",
                                   detail={"reason": result.reason, "decision_id": result.decision_id}, now=now)
            return {"refused": result.reason}
        return {"attempt_id": result.attempt_id}
    try:
        out = run_immediate(conn, work)
    except AutonomyPaused as exc:
        raise SubmitRefused("paused") from exc
    if "refused" in out:
        raise SubmitRefused(out["refused"])
    return out


def human_record_click_dispatched(conn, *, settings: Settings, attempt_id: str, now: datetime) -> bool:
    """Spec E15: the kill switch, the sentinel and pause are re-checked at
    the dispatch acknowledgement; a halt expires the attempt (SERVER) and
    releases the intent. Otherwise the unchanged 6B acknowledgement."""
    def work() -> bool:
        attempt = a._attempt(conn, attempt_id)
        _authorization_for_attempt(conn, attempt)
        if attempt_state(conn, attempt_id) != "AUTHORIZED":
            return False
        account_id = conn.execute("SELECT account_id FROM autonomy_grants WHERE id = ?",
                                  (attempt["grant_id"],)).fetchone()[0]
        halted = None
        if kill_switch_state(conn, account_id)["halted"]:
            halted = "kill_switch"
        elif sentinel_present(settings.autonomy_sentinel_path):
            halted = "sentinel"
        else:
            try:
                a._check_not_paused(conn, account_id=account_id,
                                    application_workspace_id=attempt["application_workspace_id"])
            except AutonomyPaused:
                halted = "paused"
        if halted is not None:
            append_attempt_event(conn, attempt_id=attempt_id, state="EXPIRED_UNCLICKED", source="SERVER",
                                 evidence={"halted": halted}, now=now)
            a._release(conn, attempt, now)
            return False
        return a.record_click_dispatched_in_transaction(conn, attempt_id=attempt_id, now=now)
    return run_immediate(conn, work)


# ---- evidence, results, resolution, status (spec §11, §15, §16.2) --------------------------

def record_submit_event(conn, *, attempt_id: str, event: str, detail: dict[str, Any] | None,
                        now: datetime) -> dict[str, Any]:
    """Executor evidence during an attempt (closed vocabulary, append-only)."""
    if event not in SUBMIT_EVENTS or event in ("RESULT_REPORTED", "PRE_CLICK_REFUSED", "CHALLENGE_BEFORE_SUBMIT",
                                               "CANCELLED_BEFORE_DISPATCH"):
        raise SubmitRefused("unknown_event")

    def work() -> dict[str, Any]:
        attempt = a._attempt(conn, attempt_id)
        auth = _authorization_for_attempt(conn, attempt)
        return sp.append_submit_event(conn, authorization_id=auth["id"], attempt_id=attempt_id, event=event,
                                      detail=detail, now=now)
    return run_immediate(conn, work)


def _attempt_events(conn, authorization_id: str, attempt_id: str) -> list[dict[str, Any]]:
    return [e for e in sp.submit_events(conn, authorization_id) if e["attempt_id"] in (attempt_id, None)]


def _result_evidence(evidence: dict[str, Any], events: list[dict[str, Any]]) -> ResultEvidence:
    """The extension's report, with every ADVERSE fact also taken from the
    recorded events (a report can only make the outcome less favourable)."""
    kinds = [e["event"] for e in events]
    installed = bool(evidence.get("egress_ever_installed")) or "EGRESS_INSTALLED" in kinds
    restored = bool(evidence.get("total_restored_verified")) and "TOTAL_RESTORE_FAILED" not in kinds
    if installed and "TOTAL_RESTORED" not in kinds and not evidence.get("total_restored_verified"):
        restored = False
    click = evidence.get("click_performed")
    if click not in (True, False, "UNKNOWN"):
        click = "UNKNOWN"
    if click is False and "CLICK_PERFORMED" in kinds:
        click = True
    return ResultEvidence(
        click_performed=click, egress_ever_installed=installed, total_restored_verified=restored,
        success_observed=bool(evidence.get("success_observed")),
        failure_observed=bool(evidence.get("failure_observed")),
        content_changed=bool(evidence.get("content_changed")) or "CONTENT_CHANGED_DURING_ATTEMPT" in kinds,
        matched_rule_ids=tuple(int(i) for i in evidence.get("matched_rule_ids") or ()),
        matched_rules_available=bool(evidence.get("matched_rules_available")) and MATCHED_ALLOW_RULES_REPORTED,
        cause=evidence.get("cause"))


def _record_applied(conn, *, attempt: dict[str, Any], auth: dict[str, Any], now: datetime) -> dict[str, Any]:
    """E19: the tracker's 'applied' status for this exact pack, linked to the
    attempt's intent. The tracker's own preconditions may refuse (e.g. the
    workspace is no longer 'drafted'); the submission result stands either
    way and records whether the tracker was updated."""
    grant = get_grant(conn, attempt["grant_id"])
    conn.execute("SAVEPOINT human_submit_applied")
    try:
        event = record_status_change(conn, workspace_id=auth["application_workspace_id"], new_status="applied",
                                     effective_date=now.date().isoformat(),
                                     note="Submitted via the extension (human-authorized)",
                                     submitted_pack_artifact_id=grant["binding"].get("pack_artifact_id"),
                                     commit=False, account_id=auth["account_id"])
    except ValueError as exc:
        conn.execute("ROLLBACK TO SAVEPOINT human_submit_applied")
        conn.execute("RELEASE SAVEPOINT human_submit_applied")
        return {"workflow_applied": False, "workflow_reason": str(exc)[:200]}
    conn.execute("RELEASE SAVEPOINT human_submit_applied")
    link_intent_workflow_event(conn, intent_id=attempt["intent_id"], workflow_event_id=event["id"], now=now)
    return {"workflow_applied": True, "workflow_event_id": event["id"]}


def _observation_fingerprints(conn, run_id: str) -> dict[str, Any]:
    out = {}
    for phase, key in (("PRE_SUBMIT", "pre_submit_fingerprint"), ("CHALLENGE_CLEARED", "challenge_cleared_fingerprint"),
                       ("POST_SUBMIT", "post_submit_fingerprint")):
        row = sp.latest_submit_observation(conn, run_id, phase)
        if row is not None:
            out[key] = row["observation_fingerprint"]
    return out


def report_result(conn, *, settings: Settings, attempt_id: str, evidence: dict[str, Any],
                  now: datetime) -> dict[str, Any]:
    """Spec §11.2/§15 (E8): the server decides the result from the reported
    evidence and its own recorded events, in one transaction with the
    attempt event, the intent transition, the tracker update and the
    submission-result.v1 record."""
    def work() -> dict[str, Any]:
        attempt = a._attempt(conn, attempt_id)
        auth = _authorization_for_attempt(conn, attempt)
        if attempt_state(conn, attempt_id) != "CLICK_DISPATCHED" or sp.get_submission_result(conn, attempt_id):
            raise SubmitRefused("result_already_recorded")
        events = _attempt_events(conn, auth["id"], attempt_id)
        e = _result_evidence(evidence, events)
        review = auth["review"]
        cert = submit_certified(review["target"]["adapter_id"], review["target"]["adapter_version"])
        state, proven, reason = determine_submit_result(e, cert, E1_RULE_ID)
        extra = _record_applied(conn, attempt=attempt, auth=auth, now=now) if state == "CONFIRMED_SUCCESS" else {}
        result = build_submission_result(
            attempt_id=attempt_id, authorization_id=auth["id"], grant_id=attempt["grant_id"],
            review_hash=auth["review_hash"], fill_run_id=auth["fill_run_id"], state=state,
            proven_not_submitted=proven, reason=reason, certification_id=cert.certification_id,
            events=[{"event": x["event"], "at": x["created_at"]} for x in events],
            observations=_observation_fingerprints(conn, auth["fill_run_id"]),
            matched_rule_ids=list(e.matched_rule_ids), matched_rules_available=e.matched_rules_available,
            content_changed=e.content_changed)
        result_hash = submission_result_hash(result)
        recorded = a.record_submission_result_in_transaction(
            conn, attempt_id=attempt_id, state=state, source="SERVER",
            evidence={"proven_not_submitted": proven, "reason": reason, "result_hash": result_hash, **extra},
            now=now)
        sp.insert_submission_result(conn, attempt_id=attempt_id, result=result, result_hash=result_hash, now=now)
        sp.append_submit_event(conn, authorization_id=auth["id"], attempt_id=attempt_id, event="RESULT_REPORTED",
                               detail={"state": recorded, "reason": reason}, now=now)
        return {"state": recorded, "proven_not_submitted": proven, "reason": reason, "result_hash": result_hash}
    return run_immediate(conn, work)


def resolve(conn, *, account_id: str, attempt_id: str, submitted: bool, actor: str, now: datetime) -> str:
    """The user's answer for an ambiguous attempt (§16.1)."""
    def work() -> str:
        attempt = a._attempt(conn, attempt_id)
        auth = _authorization_for_attempt(conn, attempt)
        if auth["account_id"] != account_id:
            raise SubmitRefused("not_found")
        if attempt_state(conn, attempt_id) != "SUBMISSION_AMBIGUOUS":
            raise SubmitRefused("not_ambiguous")
        state = a.resolve_ambiguous_in_transaction(conn, attempt_id=attempt_id, submitted=submitted, actor=actor,
                                                   now=now)
        if submitted:
            _record_applied(conn, attempt=attempt, auth=auth, now=now)
        return state
    try:
        return run_immediate(conn, work)
    except LookupError as exc:
        raise SubmitRefused("not_found") from exc


_STATUS = {"AUTHORIZED": "SUBMITTING", "CLICK_DISPATCHED": "SUBMITTING", "CONFIRMED_SUCCESS": "SUBMITTED",
           "SUBMISSION_AMBIGUOUS": "SUBMISSION_UNCLEAR", "SUBMISSION_FAILED": "SUBMISSION_FAILED"}


def submission_status(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                      now: datetime) -> dict[str, Any]:
    """§16.2: NOT_READY | SUBMIT_READY | SUBMITTING | CHALLENGE_WAITING |
    SUBMITTED | SUBMISSION_UNCLEAR | SUBMISSION_FAILED. Read-only."""
    from webapp.services.submit_review import _lease_live, filled_run
    attempts = sp.attempts_for_application(conn, application_workspace_id)
    latest = attempts[-1] if attempts else None
    if latest is not None and latest["state"] in _STATUS:
        status = _STATUS[latest["state"]]
        if status == "SUBMITTING":
            auth = sp.authorization_for_grant(conn, latest["grant_id"])
            kinds = [e["event"] for e in sp.submit_events(conn, auth["id"])
                     if e["event"] in ("CHALLENGE_DETECTED", "CHALLENGE_CLEARED")]
            if kinds and kinds[-1] == "CHALLENGE_DETECTED":
                status = "CHALLENGE_WAITING"
        return {"status": status, "attempt_id": latest["id"], "reason": None}
    run = filled_run(conn, application_workspace_id)
    if run is None or run["account_id"] != account_id:
        return {"status": "NOT_READY", "attempt_id": None, "reason": "no_filled_run"}
    auth = sp.authorization_for_run(conn, run["id"])
    if auth is not None:
        grant = get_grant(conn, auth["grant_id"])
        if grant["status"] == "ISSUED" and parse_utc(grant["expires_at"]) > now:
            return {"status": "SUBMITTING", "attempt_id": None, "reason": None}
        return {"status": "NOT_READY", "attempt_id": None, "reason": "authorization_used"}
    if not _lease_live(conn, run["id"], now):
        return {"status": "NOT_READY", "attempt_id": None, "reason": "lease_expired"}
    return {"status": "SUBMIT_READY", "attempt_id": None, "reason": None}
