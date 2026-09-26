"""Decision ledger, grants, reservations, intents and attempts (6B spec §9.5,
§10, §12.3, §15). Functions documented "no commit" run inside a transaction
owned by the caller (the pre-click transaction in webapp/services/autonomy.py).

Deviations from the task-11 brief, per user rulings made after the brief was
written (these override the brief's insert_decision/list_decisions bodies):
  1. AuthorizationDecision.mode / .requested_stage may be None (the
     invalid_input path) -- written as SQL NULL rather than .value/.name.
  2. AuthorizationDecision.retryable (bool) is written as 0/1 and read back
     as bool.
  3. AuthorizationDecision.completion_blockers is written to the schema's
     completion_blockers_json column as a JSON list of
     {field_key, subject, reason, prevents: <Capability name>} in the
     decision's own order, and parsed back the same way.
  4. AuthorizationDecision.decision_fingerprint is written to the schema's
     decision_fingerprint column.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from product.autonomy_contract import (
    AuthorizationContext, AuthorizationDecision, Capability, IdentityStrength, canonical_hash,
    canonical_json, to_utc_iso,
)
from product.semantic_subject_policy import subject_policy_hash
from product.standing_policy import policy_hash


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn, table: str, values: dict[str, Any]) -> dict[str, Any]:
    cols, marks = ", ".join(values), ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return dict(conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone())


# ---- decisions -------------------------------------------------------------

def _inputs_payload(ctx: AuthorizationContext) -> dict[str, Any]:
    payload = {name: getattr(ctx, name) for name in ctx.__dataclass_fields__}
    payload["standing_policy"] = policy_hash(ctx.standing_policy) if ctx.standing_policy is not None else None
    payload["subject_policy"] = subject_policy_hash(ctx.subject_policy)
    return payload


def insert_decision(conn, *, ctx: AuthorizationContext, decision: AuthorizationDecision,
                    grant_id: str | None = None, commit: bool = True) -> dict[str, Any]:
    try:
        inputs_json = canonical_json(_inputs_payload(ctx))
    except Exception:  # an invalid context (e.g. naive datetime) is still recorded
        inputs_json = json.dumps({"unserializable": True})
    row = _insert(conn, "autonomy_decisions", {
        "id": _id("dec"), "account_id": ctx.account_id,
        "application_workspace_id": ctx.application_workspace_id, "run_id": ctx.run_id,
        "mode": decision.mode.value if decision.mode is not None else None,
        "requested_stage": decision.requested_stage.name if decision.requested_stage is not None else None,
        "result": decision.result.value, "deny_reason": decision.deny_reason,
        "effective_capability": decision.effective_capability.name,
        "grantable": 1 if decision.grantable else 0,
        "reasons_json": json.dumps([[r.code, dict(r.params)] for r in decision.reasons]),
        "require_user_json": json.dumps([[i.kind, i.ref] for i in decision.require_user_items]),
        "completion_blockers_json": json.dumps([
            {"field_key": b.field_key, "subject": b.subject, "reason": b.reason, "prevents": b.prevents.name}
            for b in decision.completion_blockers
        ]),
        "retry_at": to_utc_iso(decision.retry_at) if decision.retry_at else None,
        "retryable": 1 if decision.retryable else 0,
        "inputs_json": inputs_json, "input_fingerprint": decision.input_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "engine_version": decision.engine_version, "policy_version_hash": decision.policy_version_hash,
        "subject_policy_hash": decision.subject_policy_hash, "grant_id": grant_id,
        "created_at": to_utc_iso(ctx.now) if ctx.now.tzinfo else to_utc_iso(datetime.now().astimezone()),
    })
    if commit:
        conn.commit()
    return row


def _parse_decision(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["reasons"] = [{"code": c, "params": p} for c, p in json.loads(item["reasons_json"])]
    item["require_user_items"] = [{"kind": k, "ref": r} for k, r in json.loads(item["require_user_json"])]
    item["completion_blockers"] = json.loads(item["completion_blockers_json"])
    item["retryable"] = bool(item["retryable"])
    return item


def list_decisions(conn, application_workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM autonomy_decisions WHERE application_workspace_id = ? ORDER BY seq",
        (application_workspace_id,),
    ).fetchall()
    return [_parse_decision(r) for r in rows]


def get_decision(conn, decision_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM autonomy_decisions WHERE id = ?", (decision_id,)).fetchone()
    return _parse_decision(row) if row else None


# ---- grants ----------------------------------------------------------------

def binding_fingerprint(binding: dict[str, Any]) -> str:
    return canonical_hash("autonomy-grant-binding", "v1", binding)


def _grant_event(conn, grant_id: str, status: str, reason: str | None, now: datetime) -> None:
    _insert(conn, "autonomy_grant_events", {
        "grant_id": grant_id, "status": status, "reason": reason, "created_at": to_utc_iso(now),
    })


def insert_grant(conn, *, decision_id: str, account_id: str, application_workspace_id: str,
                 stage: Capability, binding: dict[str, Any], issued_at: datetime, expires_at: datetime,
                 commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "autonomy_grants", {
        "id": _id("grant"), "decision_id": decision_id, "account_id": account_id,
        "application_workspace_id": application_workspace_id, "stage": Capability(stage).name,
        "nonce": secrets.token_hex(16), "binding_json": canonical_json(binding),
        "binding_fingerprint": binding_fingerprint(binding), "issued_at": to_utc_iso(issued_at),
        "expires_at": to_utc_iso(expires_at), "status": "ISSUED", "consumed_at": None, "revoked_reason": None,
    })
    _grant_event(conn, row["id"], "ISSUED", None, issued_at)
    if commit:
        conn.commit()
    return row


def get_grant(conn, grant_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM autonomy_grants WHERE id = ?", (grant_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["binding"] = json.loads(item["binding_json"])
    return item


def consume_grant(conn, *, grant_id: str, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE autonomy_grants SET status = 'CONSUMED', consumed_at = ? "
        "WHERE id = ? AND status = 'ISSUED' AND expires_at > ?",
        (to_utc_iso(now), grant_id, to_utc_iso(now)),
    )
    if cur.rowcount == 1:
        _grant_event(conn, grant_id, "CONSUMED", None, now)
        return True
    return False


def revoke_grant(conn, *, grant_id: str, reason: str, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE autonomy_grants SET status = 'REVOKED', revoked_reason = ? WHERE id = ? AND status = 'ISSUED'",
        (reason, grant_id),
    )
    if cur.rowcount == 1:
        _grant_event(conn, grant_id, "REVOKED", reason, now)
        return True
    return False


def revoke_issued_grants(conn, *, account_id: str, reason: str, now: datetime) -> int:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM autonomy_grants WHERE account_id = ? AND status = 'ISSUED' ORDER BY seq", (account_id,))]
    return sum(1 for grant_id in ids if revoke_grant(conn, grant_id=grant_id, reason=reason, now=now))


def expire_grants(conn, *, now: datetime) -> int:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM autonomy_grants WHERE status = 'ISSUED' AND expires_at <= ? ORDER BY seq",
        (to_utc_iso(now),))]
    for grant_id in ids:
        conn.execute("UPDATE autonomy_grants SET status = 'EXPIRED' WHERE id = ? AND status = 'ISSUED'", (grant_id,))
        _grant_event(conn, grant_id, "EXPIRED", None, now)
    return len(ids)


# ---- reservations ----------------------------------------------------------

def count_usage(conn, *, account_id: str, counter_name: str, window_key: str,
                since: datetime | None = None) -> int:
    sql = ("SELECT COALESCE(SUM(CAST(amount AS INTEGER)), 0) AS n FROM limit_reservations "
           "WHERE account_id = ? AND counter_name = ? AND window_key = ? AND status IN ('RESERVED', 'CONSUMED')")
    params: list[Any] = [account_id, counter_name, window_key]
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(to_utc_iso(since))
    return int(conn.execute(sql, params).fetchone()["n"])


def budget_usage(conn, *, account_id: str, counter_name: str, window_key: str) -> Decimal:
    rows = conn.execute(
        "SELECT amount FROM limit_reservations WHERE account_id = ? AND counter_name = ? AND window_key = ? "
        "AND status IN ('RESERVED', 'CONSUMED')", (account_id, counter_name, window_key),
    ).fetchall()
    return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))


def _reserve(conn, *, account_id, counter_name, window_key, amount: str, grant_id, attempt_id, now) -> str:
    row = _insert(conn, "limit_reservations", {
        "id": _id("res"), "account_id": account_id, "counter_name": counter_name, "window_key": window_key,
        "amount": amount, "grant_id": grant_id, "attempt_id": attempt_id, "status": "RESERVED",
        "created_at": to_utc_iso(now), "updated_at": to_utc_iso(now),
    })
    return row["id"]


def try_reserve(conn, *, account_id: str, counter_name: str, window_key: str, limit: int, now: datetime,
                since: datetime | None = None, amount: int = 1, grant_id: str | None = None,
                attempt_id: str | None = None) -> str | None:
    used = count_usage(conn, account_id=account_id, counter_name=counter_name, window_key=window_key, since=since)
    if used + amount > limit:
        return None
    return _reserve(conn, account_id=account_id, counter_name=counter_name, window_key=window_key,
                    amount=str(amount), grant_id=grant_id, attempt_id=attempt_id, now=now)


def reserve_budget(conn, *, account_id: str, counter_name: str, window_key: str, amount: Decimal,
                   grant_id: str | None, now: datetime) -> str:
    return _reserve(conn, account_id=account_id, counter_name=counter_name, window_key=window_key,
                    amount=str(amount), grant_id=grant_id, attempt_id=None, now=now)


def set_reservation_status(conn, *, reservation_id: str, status: str, now: datetime) -> None:
    conn.execute("UPDATE limit_reservations SET status = ?, updated_at = ? WHERE id = ?",
                 (status, to_utc_iso(now), reservation_id))


# ---- intents ---------------------------------------------------------------

class IntentConflict(Exception):
    pass


def workspace_identity(conn, workspace_id: str) -> tuple[str | None, IdentityStrength, bool]:
    """The durable job identity used for duplicate prevention. Lives in the
    persistence layer so the workflow 'applied' hook can use it without an
    import cycle. Weak-fallback-only identities have no strong key."""
    row = conn.execute(
        "SELECT source_record_key, canonical_url_key FROM application_workspace_job_identities "
        "WHERE application_workspace_id = ? ORDER BY rowid DESC LIMIT 1", (workspace_id,),
    ).fetchone()
    conflict = conn.execute(
        "SELECT 1 FROM application_job_identity_conflicts WHERE application_workspace_id = ?", (workspace_id,),
    ).fetchone() is not None
    if row and row["source_record_key"]:
        return row["source_record_key"], IdentityStrength.SOURCE_RECORD, conflict
    if row and row["canonical_url_key"]:
        return row["canonical_url_key"], IdentityStrength.CANONICAL_URL, conflict
    return None, IdentityStrength.WEAK, conflict


def live_intent(conn, *, account_id: str, job_identity_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM submission_intents WHERE account_id = ? AND job_identity_key = ? "
        "AND state IN ('CLAIMED', 'CONFIRMED') AND overridden = 0 ORDER BY seq DESC LIMIT 1",
        (account_id, job_identity_key),
    ).fetchone()
    return dict(row) if row else None


def overridden_confirmed_intent(conn, *, account_id: str, job_identity_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM submission_intents WHERE account_id = ? AND job_identity_key = ? "
        "AND state = 'CONFIRMED' AND overridden = 1 ORDER BY seq DESC LIMIT 1",
        (account_id, job_identity_key),
    ).fetchone()
    return dict(row) if row else None


def claim_intent(conn, *, account_id: str, job_identity_key: str, application_workspace_id: str, source: str,
                 now: datetime, state: str = "CLAIMED", workflow_event_id: str | None = None) -> dict[str, Any]:
    try:
        return _insert(conn, "submission_intents", {
            "id": _id("intent"), "account_id": account_id, "job_identity_key": job_identity_key,
            "application_workspace_id": application_workspace_id, "state": state, "source": source,
            "overridden": 0, "attempt_id": None, "workflow_event_id": workflow_event_id,
            "created_at": to_utc_iso(now), "updated_at": to_utc_iso(now),
        })
    except sqlite3.IntegrityError as exc:
        raise IntentConflict(job_identity_key) from exc


class InvalidIntentTransition(Exception):
    pass


# Only a CLAIMED intent moves (CLAIMED -> CLAIMED attaches the attempt). A
# CONFIRMED intent is cleared only by intent_overrides (spec §10.2); RELEASED
# is history.
INTENT_TRANSITIONS: dict[str, set[str]] = {"CLAIMED": {"CLAIMED", "CONFIRMED", "RELEASED"}}


def set_intent_state(conn, *, intent_id: str, state: str, now: datetime, attempt_id: str | None = None) -> None:
    row = conn.execute("SELECT state FROM submission_intents WHERE id = ?", (intent_id,)).fetchone()
    current = row["state"] if row else None
    if state not in INTENT_TRANSITIONS.get(current, set()):
        raise InvalidIntentTransition(f"{current} -> {state}")
    conn.execute(
        "UPDATE submission_intents SET state = ?, attempt_id = COALESCE(?, attempt_id), updated_at = ? WHERE id = ?",
        (state, attempt_id, to_utc_iso(now), intent_id),
    )


def add_intent_override(conn, *, intent_id: str, actor: str, reason: str, now: datetime,
                        commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "intent_overrides", {
        "id": _id("ovr"), "intent_id": intent_id, "actor": actor, "reason": reason, "created_at": to_utc_iso(now),
    })
    conn.execute("UPDATE submission_intents SET overridden = 1, updated_at = ? WHERE id = ?",
                 (to_utc_iso(now), intent_id))
    if commit:
        conn.commit()
    return row


# ---- attempts --------------------------------------------------------------

ATTEMPT_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"AUTHORIZED"},
    "AUTHORIZED": {"CLICK_DISPATCHED", "EXPIRED_UNCLICKED"},
    "CLICK_DISPATCHED": {"CONFIRMED_SUCCESS", "SUBMISSION_AMBIGUOUS", "SUBMISSION_FAILED"},
    "SUBMISSION_AMBIGUOUS": {"CONFIRMED_SUCCESS", "SUBMISSION_FAILED"},
}


class InvalidAttemptTransition(Exception):
    pass


def attempt_state(conn, attempt_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM submission_attempt_events WHERE attempt_id = ? ORDER BY seq DESC LIMIT 1", (attempt_id,),
    ).fetchone()
    return row["state"] if row else None


def append_attempt_event(conn, *, attempt_id: str, state: str, source: str, evidence: dict[str, Any],
                         now: datetime) -> dict[str, Any]:
    current = attempt_state(conn, attempt_id)
    if state not in ATTEMPT_TRANSITIONS.get(current, set()):
        raise InvalidAttemptTransition(f"{current} -> {state}")
    return _insert(conn, "submission_attempt_events", {
        "attempt_id": attempt_id, "state": state, "evidence_json": canonical_json(evidence),
        "source": source, "created_at": to_utc_iso(now),
    })


def create_attempt(conn, *, grant_id: str, intent_id: str, application_workspace_id: str,
                   run_id: str | None, now: datetime) -> dict[str, Any]:
    row = _insert(conn, "submission_attempts", {
        "id": _id("att"), "grant_id": grant_id, "intent_id": intent_id,
        "application_workspace_id": application_workspace_id, "run_id": run_id, "created_at": to_utc_iso(now),
    })
    append_attempt_event(conn, attempt_id=row["id"], state="AUTHORIZED", source="SERVER", evidence={}, now=now)
    return row


def list_attempt_events(conn, attempt_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM submission_attempt_events WHERE attempt_id = ? ORDER BY seq", (attempt_id,))
    return [dict(r) for r in rows]


def attempts_for_workspace(conn, application_workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM submission_attempts WHERE application_workspace_id = ? ORDER BY seq", (application_workspace_id,))
    return [dict(r) for r in rows]


# ---- dry run & queue -------------------------------------------------------

def record_dry_run_case(conn, *, decision_id: str, application_workspace_id: str, adapter_id: str,
                        adapter_version: str, manifest_hash: str, verification_result: str, now: datetime,
                        commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "dry_run_submission_cases", {
        "id": _id("dry"), "decision_id": decision_id, "application_workspace_id": application_workspace_id,
        "adapter_id": adapter_id, "adapter_version": adapter_version, "manifest_hash": manifest_hash,
        "verification_result": verification_result, "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def record_dry_run_agreement(conn, *, case_id: str, agreement: str, actor: str, now: datetime,
                             commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "dry_run_case_agreements", {
        "id": _id("agr"), "case_id": case_id, "agreement": agreement, "actor": actor, "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def upsert_queue_item(conn, *, application_workspace_id: str, account_id: str, next_stage: Capability,
                      next_eligible_at: datetime | None, now: datetime) -> None:
    conn.execute(
        "INSERT INTO autonomy_queue_items (application_workspace_id, account_id, next_stage, next_eligible_at, "
        "paused, updated_at) VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT(application_workspace_id) DO UPDATE SET "
        "next_stage = excluded.next_stage, next_eligible_at = excluded.next_eligible_at, updated_at = excluded.updated_at",
        (application_workspace_id, account_id, Capability(next_stage).name,
         to_utc_iso(next_eligible_at) if next_eligible_at else None, to_utc_iso(now)),
    )


def wake_queue_items(conn, *, account_id: str, now: datetime) -> int:
    cur = conn.execute(
        "UPDATE autonomy_queue_items SET next_eligible_at = ?, paused = 0, updated_at = ? WHERE account_id = ?",
        (to_utc_iso(now), to_utc_iso(now), account_id),
    )
    return cur.rowcount
