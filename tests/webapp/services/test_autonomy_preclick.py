from __future__ import annotations

from datetime import timedelta

import pytest

from product.autonomy_contract import Capability
from product.standing_policy import default_policy_document
from webapp.persistence.autonomy_authority import end_run, get_run, save_policy_version, start_run
from webapp.persistence.autonomy_ledger import (
    attempt_state, claim_intent, count_usage, get_grant, list_decisions, live_intent,
)
from webapp.services.autonomy import (
    expire_unclicked, mark_stale_dispatches_ambiguous, pre_click_commit, record_click_dispatched,
    record_submission_result, request_grant, resolve_ambiguous,
)
from webapp.services.autonomy_controls import engage_kill_switch
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401
from tests.webapp.services.test_autonomy_decide import OBS, authorize_all, manifest

T = NOW + timedelta(seconds=30)
RUN = "run_1"
IDENT = "source:greenhouse:123"


def verification(ws):
    return {e["page_field_key"]: e["value_hash"] for p in manifest(ws)["pages"] for e in p["entries"]}


def ensure_run(conn, run_id=RUN):
    if get_run(conn, run_id) is None:
        start_run(conn, account_id=ACCOUNT, started_by="SCHEDULER", now=NOW, run_id=run_id)
    return run_id


def submit_grant(conn, settings, ws):
    authorize_all(conn)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=ws,
                        stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(ws), observation=OBS,
                        run_id=ensure_run(conn))
    assert out.grant, out.decision.reasons
    return out.grant["id"]


def click(conn, settings, ws, grant_id, *, now=T, verify=None, run_id=RUN):
    return pre_click_commit(conn, settings=settings, grant_id=grant_id,
                            verification=verify if verify is not None else verification(ws),
                            now=now, observation=OBS, run_id=run_id)


def state_counts(conn):
    tables = ("autonomy_decisions", "limit_reservations", "submission_intents", "submission_attempts",
              "autonomy_grant_events")
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}


def no_authority_created(conn, before):
    after = state_counts(conn)
    for table in ("limit_reservations", "submission_intents", "submission_attempts"):
        assert after[table] == before[table], table


def test_happy_path_authorizes_once(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    result = click(conn, settings, seeded, gid)
    assert result.authorized and result.attempt_id
    assert attempt_state(conn, result.attempt_id) == "AUTHORIZED"
    assert get_grant(conn, gid)["status"] == "CONSUMED"
    ident = list_decisions(conn, seeded)[-1]
    assert ident["grant_id"] == gid
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key=IDENT)["attempt_id"] == result.attempt_id
    for name, key in (("submit_per_day", "2026-09-24"), ("submit_per_run", RUN), ("submit_per_employer_30d", "name:acme")):
        assert count_usage(conn, account_id=ACCOUNT, counter_name=name, window_key=key) == 1, name
    again = click(conn, settings, seeded, gid)
    assert not again.authorized and again.reason == "duplicate"


def test_verification_mismatch_revokes_and_leaves_no_authority(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    before = state_counts(conn)
    result = click(conn, settings, seeded, gid, verify={"email": "sha256:other"})
    assert (result.authorized, result.reason) == (False, "stale_binding")
    assert get_grant(conn, gid)["status"] == "REVOKED"
    no_authority_created(conn, before)
    assert click(conn, settings, seeded, gid).authorized is False  # the old grant is unusable


def test_policy_change_after_grant_is_drift(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    doc = default_policy_document("Europe/London")
    doc["limits"]["fill_per_day"] = 9
    save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)
    before = state_counts(conn)
    assert click(conn, settings, seeded, gid).reason == "stale_binding"
    no_authority_created(conn, before)


def test_different_run_is_drift(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    ensure_run(conn, "run_2")
    assert click(conn, settings, seeded, gid, run_id="run_2").reason == "stale_binding"


def test_kill_switch_before_commit_means_no_click(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    before = state_counts(conn)
    assert click(conn, settings, seeded, gid).reason == "kill_switch"
    no_authority_created(conn, before)
    assert get_grant(conn, gid)["status"] == "REVOKED"


def test_sentinel_checked_synchronously(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    settings.autonomy_sentinel_path.write_text("halt")
    before = state_counts(conn)
    assert click(conn, settings, seeded, gid).reason == "kill_switch"
    no_authority_created(conn, before)


def test_live_intent_blocks(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    claim_intent(conn, account_id=ACCOUNT, job_identity_key=IDENT, application_workspace_id=seeded,
                 source="HUMAN_APPLIED", state="CONFIRMED", now=NOW)
    conn.commit()
    before = state_counts(conn)
    assert click(conn, settings, seeded, gid).reason == "duplicate"
    no_authority_created(conn, before)
    assert get_grant(conn, gid)["status"] == "REVOKED"


def test_expired_grant_not_consumable(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    before = state_counts(conn)
    assert click(conn, settings, seeded, gid, now=NOW + timedelta(seconds=121)).reason == "grant_not_consumable"
    no_authority_created(conn, before)


def test_pause_writes_nothing(conn, settings, seeded):
    from webapp.services.autonomy import AutonomyPaused
    from webapp.services.autonomy_controls import pause
    gid = submit_grant(conn, settings, seeded)
    pause(conn, account_id=ACCOUNT, scope_type="APPLICATION", scope_id=seeded, actor="u", reason="r", now=NOW)
    before = state_counts(conn)
    with pytest.raises(AutonomyPaused):
        click(conn, settings, seeded, gid)
    assert state_counts(conn) == before
    assert get_grant(conn, gid)["status"] == "ISSUED"


@pytest.mark.parametrize("run_id", [None, "run_missing", "run_ended", "run_other_account"])
def test_submit_without_a_valid_active_run_fails_closed(conn, settings, seeded, run_id):
    from webapp.persistence.accounts import create_account
    from webapp.services.autonomy import RunRequired
    gid = submit_grant(conn, settings, seeded)
    if run_id == "run_ended":
        ensure_run(conn, run_id)
        end_run(conn, run_id=run_id, end_reason="done", now=NOW)
    elif run_id == "run_other_account":
        other = create_account(conn, display_name="Other")["id"]
        start_run(conn, account_id=other, started_by="USER", now=NOW, run_id=run_id)
    before = state_counts(conn)
    with pytest.raises(RunRequired):
        click(conn, settings, seeded, gid, run_id=run_id)
    assert state_counts(conn) == before
    assert get_grant(conn, gid)["status"] == "ISSUED"
    with pytest.raises(RunRequired):
        request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                      stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(seeded), observation=OBS,
                      run_id=run_id)
    assert state_counts(conn) == before


@pytest.mark.parametrize("grant_id", ["grant_missing", "fill"])
def test_unknown_or_non_submit_grant_leaves_zero_state(conn, settings, seeded, grant_id):
    # Carried acceptance: an invalid identity at the pre-click boundary leaves
    # zero new decision, grant-consumption, reservation, intent and attempt state.
    authorize_all(conn)
    if grant_id == "fill":
        grant_id = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                                 stage=Capability.FILL, now=NOW, fill_manifest=manifest(seeded),
                                 observation=OBS).grant["id"]
    before = state_counts(conn)
    with pytest.raises(ValueError):
        click(conn, settings, seeded, grant_id, run_id=ensure_run(conn))
    assert state_counts(conn) == before


def test_failure_after_reservation_rolls_everything_back(conn, settings, seeded, monkeypatch):
    from webapp.services import autonomy
    gid = submit_grant(conn, settings, seeded)
    before = state_counts(conn)

    def boom(*a, **kw):
        raise RuntimeError("crash between reservation and intent")
    monkeypatch.setattr(autonomy, "claim_intent", boom)
    with pytest.raises(RuntimeError):
        click(conn, settings, seeded, gid)
    assert state_counts(conn) == before
    assert get_grant(conn, gid)["status"] == "ISSUED"


def test_dispatch_ack_and_unclicked_expiry(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    assert record_click_dispatched(conn, attempt_id=attempt, now=T + timedelta(seconds=61)) is False
    assert attempt_state(conn, attempt) == "EXPIRED_UNCLICKED"
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key=IDENT) is None
    assert count_usage(conn, account_id=ACCOUNT, counter_name="submit_per_day", window_key="2026-09-24") == 0


def test_expire_unclicked_sweep(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    assert expire_unclicked(conn, now=T + timedelta(seconds=30)) == 0
    assert expire_unclicked(conn, now=T + timedelta(seconds=60)) == 1
    assert attempt_state(conn, attempt) == "EXPIRED_UNCLICKED"


def test_kill_switch_after_commit_lets_authorized_click_complete(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=T + timedelta(seconds=1))
    assert record_click_dispatched(conn, attempt_id=attempt, now=T + timedelta(seconds=2)) is True


def test_results_and_ambiguity(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    record_click_dispatched(conn, attempt_id=attempt, now=T)
    recorded = record_submission_result(conn, attempt_id=attempt, state="SUBMISSION_FAILED", source="EXECUTOR",
                                        evidence={"page": "timeout"}, now=T)
    assert recorded == "SUBMISSION_AMBIGUOUS"  # failure without proof is never retried
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key=IDENT)["state"] == "CLAIMED"
    assert resolve_ambiguous(conn, attempt_id=attempt, submitted=True, actor="u", now=T) == "CONFIRMED_SUCCESS"
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key=IDENT)["state"] == "CONFIRMED"
    assert count_usage(conn, account_id=ACCOUNT, counter_name="submit_per_day", window_key="2026-09-24") == 1


def test_proven_failure_releases(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    record_click_dispatched(conn, attempt_id=attempt, now=T)
    assert record_submission_result(conn, attempt_id=attempt, state="SUBMISSION_FAILED", source="EXECUTOR",
                                    evidence={"proven_not_submitted": True, "errors": ["phone required"]}, now=T) == "SUBMISSION_FAILED"
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key=IDENT) is None


def test_dispatched_without_result_becomes_ambiguous(conn, settings, seeded):
    gid = submit_grant(conn, settings, seeded)
    attempt = click(conn, settings, seeded, gid).attempt_id
    record_click_dispatched(conn, attempt_id=attempt, now=T)
    assert mark_stale_dispatches_ambiguous(conn, now=T + timedelta(minutes=5), result_timeout=timedelta(minutes=10)) == 0
    assert mark_stale_dispatches_ambiguous(conn, now=T + timedelta(minutes=11), result_timeout=timedelta(minutes=10)) == 1
    assert attempt_state(conn, attempt) == "SUBMISSION_AMBIGUOUS"


def test_revoked_grant_releases_its_budget_reservations(conn, settings, seeded):
    from decimal import Decimal
    authorize_all(conn)
    doc = default_policy_document("Europe/London")
    doc["limits"]["budgets"] = {"LLM": {"per_day": "1.00", "per_application": "0.50"}}
    save_policy_version(conn, account_id=ACCOUNT, doc=doc, created_by="u", now=NOW)
    out = request_grant(conn, settings=settings, account_id=ACCOUNT, application_workspace_id=seeded,
                        stage=Capability.SUBMIT, now=NOW, fill_manifest=manifest(seeded), observation=OBS,
                        run_id=ensure_run(conn), cost_estimates={"LLM": Decimal("0.10")})
    gid = out.grant["id"]
    statuses = lambda: sorted(r["status"] for r in conn.execute(
        "SELECT status FROM limit_reservations WHERE grant_id = ?", (gid,)))
    assert statuses() == ["RESERVED", "RESERVED"]
    engage_kill_switch(conn, account_id=ACCOUNT, actor="u", reason="stop", now=NOW)
    assert click(conn, settings, seeded, gid).reason == "kill_switch"
    assert statuses() == ["RELEASED", "RELEASED"]
