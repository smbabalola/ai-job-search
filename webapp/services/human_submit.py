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

from webapp.config import Settings
from webapp.persistence import submit as sp
from webapp.persistence.autonomy_ledger import get_grant, revoke_grant
from webapp.services.autonomy import AutonomyPaused, request_human_submit_grant
from webapp.services.autonomy_controls import run_immediate
from webapp.services.fill_runs import target_observation
from webapp.services.submit_review import SubmitRefused, assemble, gate_reasons, human_manifest

__all__ = ["SubmitRefused", "authorize", "cancel_authorization"]


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
        raise SubmitRefused("not_cancellable")
    return run_immediate(conn, work)
