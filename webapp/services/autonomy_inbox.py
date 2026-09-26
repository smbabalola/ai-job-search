"""The 6C exception surface (spec §6.2, §9, §10.2): derived inbox, occurrence-
keyed notifications, reconciliation, wake-only answer propagation and
retry eligibility. Nothing here decides or authorizes; it records what the
user needs to see and wakes the scheduler when something changed."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from product.autonomy_contract import Reach, normalized_employer_key
from product.prepare_steps import MAX_ATTEMPTS_PER_CYCLE
from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.application_identity import get_search_workspace_for_application
from webapp.persistence.workspaces import get_workspace
from webapp.services.autonomy_controls import run_immediate

ACTIONABLE = frozenset({"NEEDS_USER", "CANDIDATE_QUESTION"})
INFORMATIONAL = frozenset({"PREPARED", "BLOCKED", "OPERATIONAL_ERROR"})


class RetryNotEligible(Exception):
    pass


# ---- notifications ----------------------------------------------------------

def notification_key(kind: str, subject_type: str, subject_id: str, reason: str, fingerprint: str) -> str:
    return f"{kind}:{subject_type}:{subject_id}:{reason}:{fingerprint}"


def notify_outcome(conn, *, account_id: str, subject_type: str, subject_id: str, kind: str, reason: str,
                   fingerprint: str, detail: dict[str, Any], now: datetime) -> bool:
    """No commit. Repeated ticks never re-notify the same occurrence."""
    return ap.create_notification(conn, account_id=account_id,
                                  key=notification_key(kind, subject_type, subject_id, reason, fingerprint),
                                  kind=kind, subject_type=subject_type, subject_id=subject_id,
                                  detail={"reason": reason, **detail}, now=now)


def inbox_summary(conn, account_id: str) -> dict[str, int]:
    items = ap.open_notifications(conn, account_id)
    actionable = sum(1 for n in items if n["kind"] in ACTIONABLE)
    unseen = sum(1 for n in items if n["kind"] in INFORMATIONAL and not n["seen"])
    return {"badge": actionable + unseen, "actionable": actionable, "informational_unseen": unseen}


def build_inbox(conn, *, account_id: str) -> dict[str, list[dict[str, Any]]]:
    """Derived and read-only. Candidate questions carry every current reason
    of their screening; application entries carry their recorded detail."""
    view: dict[str, list[dict[str, Any]]] = {"needs_answer": [], "informational": [], "ready": []}
    exceptions = {e["candidate_id"]: e for e in ap.open_candidate_exceptions(conn, account_id)}
    for note in ap.open_notifications(conn, account_id):
        entry = dict(note)
        if note["kind"] == "CANDIDATE_QUESTION":
            exception = exceptions.get(note["subject_id"])
            if exception is None:
                continue  # answered; reconciliation resolves it
            screening = ap.get_screening(conn, exception["screening_id"])
            entry["exception_id"] = exception["id"]
            entry["items"] = exception["items"]
            entry["reasons"] = screening["reasons"] if screening else []
        if note["kind"] in ACTIONABLE:
            view["needs_answer"].append(entry)
        elif note["kind"] == "PREPARED":
            view["ready"].append(entry)
        else:
            view["informational"].append(entry)
    return view


def mark_inbox_seen(conn, *, account_id: str, now: datetime) -> int:
    def work() -> int:
        return sum(1 for n in ap.open_notifications(conn, account_id)
                   if ap.mark_seen(conn, account_id=account_id, key=n["key"], now=now))
    return run_immediate(conn, work)


def _item_woken(conn, subject_type: str, subject_id: str) -> bool:
    table, key = ap.QUEUES[subject_type]
    row = conn.execute(f"SELECT next_eligible_at FROM {table} WHERE {key} = ?", (subject_id,)).fetchone()
    return row is None or row["next_eligible_at"] is not None


def reconcile_notifications(conn, *, account_id: str, now: datetime) -> int:
    """RESOLVED only when the derived condition is gone: a candidate question
    once answered; any other application/candidate notification once its item
    has been woken (an answer, retry, resume, policy/authority or input change
    happened) — if the condition recurs, the tick notifies it afresh."""
    def work() -> int:
        resolved = 0
        for note in ap.open_notifications(conn, account_id):
            if note["kind"] == "CANDIDATE_QUESTION":
                current = ap.current_candidate_exception(conn, note["subject_id"])
                gone = current is None or current["resolution"] is not None
            elif note["subject_type"] in ap.QUEUES:
                gone = _item_woken(conn, note["subject_type"], note["subject_id"])
            else:
                gone = False
            if gone and ap.resolve_notification(conn, account_id=account_id, key=note["key"], now=now):
                resolved += 1
        return resolved
    return run_immediate(conn, work)


# ---- propagation -------------------------------------------------------------

def _waiting_on(conn, workspace_id: str, subject: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM application_blockers WHERE workspace_id = ? AND semantic_subject_key = ? AND status = 'open'",
        (workspace_id, subject)).fetchone() is not None


def _in_reach(conn, account_id: str, workspace_id: str, reach: str, scope_id: str | None) -> bool:
    if reach == Reach.ACCOUNT.value:
        return True
    if reach == Reach.SEARCH_WORKSPACE.value:
        return scope_id is not None and get_search_workspace_for_application(conn, workspace_id) == scope_id
    workspace = get_workspace(conn, workspace_id, account_id=account_id) or {}
    return scope_id is not None and normalized_employer_key(workspace.get("company")) == scope_id


def propagate_answer(conn, *, account_id: str, subject: str, reach: str, scope_id: str | None,
                     now: datetime) -> int:
    """No commit; runs in the answer's transaction. Wake-only: it never writes
    a sibling's resolution, decision, answer or confirmation."""
    woken = 0
    for workspace_id in ap.enrolled_applications(conn, account_id):
        if _waiting_on(conn, workspace_id, subject) and _in_reach(conn, account_id, workspace_id, reach, scope_id):
            woken += int(ap.wake(conn, queue="APPLICATION", item_id=workspace_id, now=now))
    return woken


def answer_blocker(conn, *, account_id: str, workspace_id: str, blocker_id: str, request_id: str,
                   answer_value: Any, answer_scope: str, resolved_by: str, reusable: bool, subject: str | None,
                   reach: str | None, scope_id: str | None, context: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Resolve, optionally save a reusable 6B answer, propagate and wake — in
    ONE transaction."""
    from webapp.persistence.application_blockers import resolve_application_blocker
    from webapp.persistence.autonomy_answers import approve_answer
    from webapp.services.decision_policy import validate_answer_scope

    def work() -> dict[str, Any]:
        validate_answer_scope(conn, workspace_id=workspace_id, answer_scope=answer_scope)
        resolution = resolve_application_blocker(conn, blocker_id=blocker_id, request_id=request_id,
                                                 answer_value=answer_value, answer_scope=answer_scope,
                                                 resolved_by=resolved_by, commit=False)
        answer = None
        if reusable:
            value = answer_value.get("value") if isinstance(answer_value, dict) else answer_value
            answer = approve_answer(conn, account_id=account_id, subject=subject, value=value, reach=Reach(reach),
                                    scope_id=scope_id, context=context, basis={"kind": "USER_ASSERTION"},
                                    approved_by=resolved_by, now=now,
                                    source_blocker_resolution_id=resolution["id"], commit=False)
            propagate_answer(conn, account_id=account_id, subject=subject, reach=reach, scope_id=scope_id, now=now)
        ap.wake(conn, queue="APPLICATION", item_id=workspace_id, now=now)
        return {"resolution": resolution, "approved_answer": answer}
    return run_immediate(conn, work)


# ---- retry eligibility -----------------------------------------------------------

def retry_failure(conn, *, account_id: str, subject_type: str, subject_id: str, step_kind: str, actor: str,
                  now: datetime) -> dict[str, Any]:
    """Only an exhausted TRANSIENT cycle of the step's current input
    fingerprint, with no cycle already open, may be retried (one-shot)."""
    def work() -> dict[str, Any]:
        terminal = [r for r in ap.attempt_rows(conn, subject_type, subject_id)
                    if r["step_kind"] == step_kind and r["event"] != "STARTED"]
        if not terminal:
            raise RetryNotEligible("no failed attempt to retry")
        last = terminal[-1]
        if last["event"] not in ("FAILED", "ABANDONED") or (last["event"] == "FAILED"
                                                            and last["error_class"] != "TRANSIENT"):
            raise RetryNotEligible("only an exhausted transient failure can be retried")
        fingerprint = last["input_fingerprint"]
        failures = ap.cycle_failures(conn, subject_type=subject_type, subject_id=subject_id, step_kind=step_kind,
                                     input_fingerprint=fingerprint, retry_request_id=last["retry_request_id"])
        if failures < MAX_ATTEMPTS_PER_CYCLE:
            raise RetryNotEligible("the automatic retry cycle is still running")
        latest = ap.latest_retry_request(conn, subject_type=subject_type, subject_id=subject_id,
                                         step_kind=step_kind, input_fingerprint=fingerprint)
        if latest is not None and latest["id"] != last["retry_request_id"]:
            # a newer request whose cycle has not produced its own terminal attempt yet
            raise RetryNotEligible("a retry is already open for this step")
        request = ap.record_retry_request(conn, account_id=account_id, subject_type=subject_type,
                                          subject_id=subject_id, step_kind=step_kind,
                                          input_fingerprint=fingerprint, actor=actor, now=now)
        ap.wake(conn, queue=subject_type, item_id=subject_id, now=now)
        return request
    return run_immediate(conn, work)
