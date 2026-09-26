from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from product.autonomy_contract import Capability, RepresentationRequirement
from product.autonomy_gate import evaluate_authorization
from webapp.persistence.autonomy_ledger import (
    IntentConflict, InvalidAttemptTransition, add_intent_override, append_attempt_event,
    attempt_state, budget_usage, claim_intent, consume_grant, count_usage, create_attempt,
    expire_grants, get_grant, insert_decision, insert_grant, list_decisions, live_intent,
    reserve_budget, revoke_issued_grants, set_intent_state, try_reserve,
)
from tests.product.autonomy_fixtures import make_ctx
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401


def _decision(conn, ws, **overrides):
    ctx = make_ctx(account_id=ACCOUNT, application_workspace_id=ws, **overrides)
    row = insert_decision(conn, ctx=ctx, decision=evaluate_authorization(ctx))
    return row


def _grant(conn, ws, *, stage=Capability.SUBMIT, ttl=timedelta(seconds=120)):
    decision = _decision(conn, ws)
    return insert_grant(conn, decision_id=decision["id"], account_id=ACCOUNT, application_workspace_id=ws,
                        stage=stage, binding={"pack": "art_pack"}, issued_at=NOW, expires_at=NOW + ttl)


def test_decisions_recorded_in_seq_order_with_reasons_and_hashed_inputs(conn):
    ws = make_workspace(conn)
    _decision(conn, ws)
    _decision(conn, ws, kill_switch_engaged=True)
    rows = list_decisions(conn, ws)
    assert [r["result"] for r in rows] == ["ALLOW", "DENY"]
    assert rows[1]["deny_reason"] == "kill_switch"
    assert any(r["code"] == "kill_switch" for r in rows[1]["reasons"])
    assert '"subject_policy":"sha256:' in rows[0]["inputs_json"]  # policies stored by hash, not inline


def test_invalid_input_decision_stores_null_mode_and_stage(conn):
    ws = make_workspace(conn)
    # A mode/requested_stage that aren't valid Mode/Capability instances make
    # the gate's decision itself carry None for both (product/autonomy_gate.py
    # _decision_mode/_decision_stage) -- a naive `now` alone still leaves a
    # valid Mode/Capability on the decision, so it would not exercise the
    # NULL-mode/stage path this test targets.
    ctx = make_ctx(account_id=ACCOUNT, application_workspace_id=ws, mode="not_a_mode",
                    requested_stage="not_a_stage")
    decision = evaluate_authorization(ctx)
    assert decision.mode is None and decision.requested_stage is None
    assert decision.deny_reason == "invalid_input"
    row = insert_decision(conn, ctx=ctx, decision=decision)
    assert row["mode"] is None
    assert row["requested_stage"] is None
    stored = list_decisions(conn, ws)[0]
    assert stored["mode"] is None and stored["requested_stage"] is None


def test_completion_blockers_and_fingerprint_round_trip(conn):
    ws = make_workspace(conn)
    requirement = RepresentationRequirement(
        key="employment.notice_period", subject="employment.notice_period", required=True,
        evidence_available=False, candidates=(),
    )
    ctx = make_ctx(account_id=ACCOUNT, application_workspace_id=ws, requested_stage=Capability.FILL,
                    requirements=(requirement,))
    decision = evaluate_authorization(ctx)
    assert len(decision.completion_blockers) == 1
    blocker = decision.completion_blockers[0]
    assert blocker.field_key == "employment.notice_period"
    assert blocker.prevents == Capability.SUBMIT
    row = insert_decision(conn, ctx=ctx, decision=decision)
    assert row["decision_fingerprint"] == decision.decision_fingerprint
    stored = list_decisions(conn, ws)[0]
    assert stored["completion_blockers"] == [{
        "field_key": blocker.field_key, "subject": blocker.subject,
        "reason": blocker.reason, "prevents": "SUBMIT",
    }]
    assert stored["decision_fingerprint"] == decision.decision_fingerprint


def test_retryable_round_trips_for_limit_denial(conn):
    from product.autonomy_contract import CounterState

    ws = make_workspace(conn)
    ctx = make_ctx(account_id=ACCOUNT, application_workspace_id=ws, counters=(
        CounterState(name="submit_per_day", stage=Capability.SUBMIT, used=1, limit=1, retry_at=None),
    ))
    decision = evaluate_authorization(ctx)
    assert decision.retryable is True
    row = insert_decision(conn, ctx=ctx, decision=decision)
    assert row["retryable"] == 1  # insert_decision returns the raw row; 0/1 as stored
    stored = list_decisions(conn, ws)[0]
    assert stored["retryable"] is True  # list_decisions/get_decision parse it back to bool


def test_grant_consumed_exactly_once_and_not_after_expiry(conn):
    ws = make_workspace(conn)
    g = _grant(conn, ws)
    assert consume_grant(conn, grant_id=g["id"], now=NOW + timedelta(seconds=10)) is True
    assert consume_grant(conn, grant_id=g["id"], now=NOW + timedelta(seconds=11)) is False
    g2 = _grant(conn, ws)
    assert consume_grant(conn, grant_id=g2["id"], now=NOW + timedelta(seconds=121)) is False
    assert expire_grants(conn, now=NOW + timedelta(seconds=121)) == 1
    assert get_grant(conn, g2["id"])["status"] == "EXPIRED"


def test_revoke_issued_only(conn):
    ws = make_workspace(conn)
    consumed, issued = _grant(conn, ws), _grant(conn, ws)
    consume_grant(conn, grant_id=consumed["id"], now=NOW)
    assert revoke_issued_grants(conn, account_id=ACCOUNT, reason="kill_switch", now=NOW) == 1
    assert get_grant(conn, consumed["id"])["status"] == "CONSUMED"
    assert get_grant(conn, issued["id"])["status"] == "REVOKED"


def test_reservations_respect_limit_and_window(conn):
    kw = dict(account_id=ACCOUNT, counter_name="submit_per_day", window_key="2026-09-24", limit=2, now=NOW)
    assert try_reserve(conn, **kw) and try_reserve(conn, **kw)
    assert try_reserve(conn, **kw) is None
    assert count_usage(conn, account_id=ACCOUNT, counter_name="submit_per_day", window_key="2026-09-24") == 2
    assert try_reserve(conn, **{**kw, "window_key": "2026-09-25"})
    old = dict(account_id=ACCOUNT, counter_name="submit_per_employer_30d", window_key="name:acme", limit=1)
    assert try_reserve(conn, now=NOW - timedelta(days=31), since=NOW - timedelta(days=61), **old)
    assert try_reserve(conn, now=NOW, since=NOW - timedelta(days=30), **old)  # 31-day-old one is outside the window
    ws = make_workspace(conn)
    g = _grant(conn, ws)
    reserve_budget(conn, account_id=ACCOUNT, counter_name="budget:LLM:day", window_key="2026-09-24",
                   amount=Decimal("0.25"), grant_id=g["id"], now=NOW)
    assert budget_usage(conn, account_id=ACCOUNT, counter_name="budget:LLM:day", window_key="2026-09-24") == Decimal("0.25")


def test_intents_unique_override_and_release(conn):
    ws = make_workspace(conn)
    kw = dict(account_id=ACCOUNT, job_identity_key="source:x:1", application_workspace_id=ws, now=NOW)
    first = claim_intent(conn, source="AUTONOMOUS", **kw)
    with pytest.raises(IntentConflict):
        claim_intent(conn, source="AUTONOMOUS", **kw)
    set_intent_state(conn, intent_id=first["id"], state="CONFIRMED", now=NOW)
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key="source:x:1")["state"] == "CONFIRMED"
    add_intent_override(conn, intent_id=first["id"], actor="u", reason="apply again", now=NOW)
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key="source:x:1") is None
    second = claim_intent(conn, source="AUTONOMOUS", **kw)
    set_intent_state(conn, intent_id=second["id"], state="RELEASED", now=NOW)
    claim_intent(conn, source="AUTONOMOUS", **kw)


def test_workspace_identity_strength(conn):
    from product.autonomy_contract import IdentityStrength
    from webapp.persistence.application_identity import save_application_identity
    from webapp.persistence.autonomy_ledger import workspace_identity
    ws = make_workspace(conn)
    assert workspace_identity(conn, ws) == (None, IdentityStrength.WEAK, False)
    save_application_identity(conn, application_workspace_id=ws, source_record={
        "company": "Acme", "title": "Eng", "location": "UK", "source_url": "https://boards.greenhouse.io/acme/jobs/1"})
    key, strength, conflict = workspace_identity(conn, ws)
    assert key.startswith("url:") and strength is IdentityStrength.CANONICAL_URL and conflict is False


def test_attempt_lifecycle_transitions(conn):
    ws = make_workspace(conn)
    g = _grant(conn, ws)
    intent = claim_intent(conn, account_id=ACCOUNT, job_identity_key="source:x:1", application_workspace_id=ws,
                          source="AUTONOMOUS", now=NOW)
    attempt = create_attempt(conn, grant_id=g["id"], intent_id=intent["id"], application_workspace_id=ws,
                             run_id=None, now=NOW)
    assert attempt_state(conn, attempt["id"]) == "AUTHORIZED"
    with pytest.raises(InvalidAttemptTransition):
        append_attempt_event(conn, attempt_id=attempt["id"], state="CONFIRMED_SUCCESS", source="EXECUTOR", evidence={}, now=NOW)
    append_attempt_event(conn, attempt_id=attempt["id"], state="CLICK_DISPATCHED", source="EXECUTOR", evidence={}, now=NOW)
    append_attempt_event(conn, attempt_id=attempt["id"], state="SUBMISSION_AMBIGUOUS", source="SERVER", evidence={}, now=NOW)
    with pytest.raises(InvalidAttemptTransition):
        append_attempt_event(conn, attempt_id=attempt["id"], state="CLICK_DISPATCHED", source="EXECUTOR", evidence={}, now=NOW)
    append_attempt_event(conn, attempt_id=attempt["id"], state="CONFIRMED_SUCCESS", source="USER", evidence={}, now=NOW)
    assert attempt_state(conn, attempt["id"]) == "CONFIRMED_SUCCESS"


def test_intent_state_only_leaves_claimed(conn):
    # Spec §10.2: a CONFIRMED intent permanently suppresses autonomous
    # submission unless overridden, and a RELEASED intent is history -- neither
    # may be moved by set_intent_state.
    from webapp.persistence.autonomy_ledger import InvalidIntentTransition
    ws = make_workspace(conn)
    kw = dict(account_id=ACCOUNT, application_workspace_id=ws, source="AUTONOMOUS", now=NOW)
    claimed = claim_intent(conn, job_identity_key="source:x:1", **kw)
    set_intent_state(conn, intent_id=claimed["id"], state="CLAIMED", now=NOW, attempt_id="att_1")
    set_intent_state(conn, intent_id=claimed["id"], state="CONFIRMED", now=NOW)
    for state in ("RELEASED", "CLAIMED", "CONFIRMED"):
        with pytest.raises(InvalidIntentTransition):
            set_intent_state(conn, intent_id=claimed["id"], state=state, now=NOW)
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key="source:x:1")["state"] == "CONFIRMED"
    released = claim_intent(conn, job_identity_key="source:x:2", **kw)
    set_intent_state(conn, intent_id=released["id"], state="RELEASED", now=NOW)
    with pytest.raises(InvalidIntentTransition):
        set_intent_state(conn, intent_id=released["id"], state="CLAIMED", now=NOW)
    with pytest.raises(InvalidIntentTransition):
        set_intent_state(conn, intent_id="intent_missing", state="RELEASED", now=NOW)


def test_expire_grants_counts_and_logs_only_changed_rows(conn):
    ws = make_workspace(conn)
    g = _grant(conn, ws)
    assert expire_grants(conn, now=NOW + timedelta(seconds=121)) == 1
    assert expire_grants(conn, now=NOW + timedelta(seconds=122)) == 0
    events = conn.execute("SELECT status FROM autonomy_grant_events WHERE grant_id = ? ORDER BY seq",
                          (g["id"],)).fetchall()
    assert [e["status"] for e in events] == ["ISSUED", "EXPIRED"]


@pytest.mark.parametrize("account_id, workspace", [
    (None, "ws"), ("acct_missing", "ws"), (ACCOUNT, None), (ACCOUNT, "ws_missing"),
])
def test_decision_without_valid_identity_fails_before_any_authority(conn, account_id, workspace):
    # A decision whose account/application identity is missing or unknown is a
    # programming/integration error: persisting it fails, and nothing that
    # carries executable authority can be created from it.
    import sqlite3
    ws = make_workspace(conn)
    ctx = make_ctx(account_id=account_id, application_workspace_id=ws if workspace == "ws" else workspace)
    with pytest.raises(sqlite3.IntegrityError):
        insert_decision(conn, ctx=ctx, decision=evaluate_authorization(ctx))
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) AS n FROM autonomy_decisions").fetchone()["n"] == 0
    with pytest.raises(sqlite3.IntegrityError):  # no decision row -> no grant can reference one
        insert_grant(conn, decision_id="dec_missing", account_id=ACCOUNT, application_workspace_id=ws,
                     stage=Capability.SUBMIT, binding={}, issued_at=NOW, expires_at=NOW + timedelta(seconds=60))
    conn.rollback()
    for table in ("autonomy_decisions", "autonomy_grants", "autonomy_grant_events", "limit_reservations",
                  "submission_intents", "submission_attempts"):
        assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0, table
