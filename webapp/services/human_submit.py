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
           "human_record_click_dispatched"]

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
