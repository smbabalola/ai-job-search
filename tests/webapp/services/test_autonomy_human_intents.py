from __future__ import annotations

from datetime import timedelta

from tests.webapp.fixtures.application_material import completion_ready_pack_payload
from webapp.persistence import migrations as migrations_module
from webapp.persistence import workflow as workflow_module
from webapp.persistence.application_identity import save_application_identity
from webapp.persistence.artifacts import save_artifact
from webapp.persistence.autonomy_ledger import attempt_state, live_intent, workspace_identity
from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import apply_migrations
from webapp.persistence.workflow import record_status_change
from webapp.persistence.workspaces import create_workspace
from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from tests.webapp.services.test_autonomy_context import seeded, settings  # noqa: F401

RECORD = {"source": "greenhouse", "source_record_id": "77", "company": "Acme", "title": "Eng", "location": "UK"}
BACKFILL = "017_autonomy_human_intent_backfill"


def _ws(conn, record=RECORD, maker=make_workspace):
    ws = maker(conn)
    save_application_identity(conn, application_workspace_id=ws, source_record=record)
    conn.commit()
    return ws


def mark_applied(conn, ws):
    """The real path to 'applied': Gate 4 drafted with a ready pack, then applied."""
    pack = save_artifact(conn, workspace_id=ws, artifact_type="application_pack",
                         payload=completion_ready_pack_payload(ws))
    record_status_change(conn, workspace_id=ws, new_status="drafted", effective_date="2026-09-23",
                         submitted_pack_artifact_id=pack["id"], _allow_drafted=True)
    return record_status_change(conn, workspace_id=ws, new_status="applied", effective_date="2026-09-24",
                                submitted_pack_artifact_id=pack["id"])


def add_handoff_confirmation(conn, ws, suffix="1"):
    pack = save_artifact(conn, workspace_id=ws, artifact_type="application_pack", payload={"schema_version": "x"})
    conn.execute(
        "INSERT INTO handoff_sessions (id, account_id, workspace_id, pack_artifact_id, target_url, target_domain, "
        "ats_adapter_id, ats_adapter_version, started_at, status) VALUES "
        f"('hs{suffix}', ?, ?, ?, 'https://boards.greenhouse.io/acme/jobs/77', 'boards.greenhouse.io', 'greenhouse', "
        "'1', '2026-08-01T00:00:00+00:00', 'user_confirmed_submitted')", (ACCOUNT, ws, pack["id"]))
    conn.execute("INSERT INTO submission_confirmations (id, handoff_session_id, created_at) "
                 f"VALUES ('sc{suffix}', 'hs{suffix}', '2026-08-01T00:00:00+00:00')")
    conn.commit()


def test_marking_applied_creates_confirmed_human_intent(conn):
    ws = _ws(conn)
    event = mark_applied(conn, ws)
    key, _, _ = workspace_identity(conn, ws)
    intent = live_intent(conn, account_id=ACCOUNT, job_identity_key=key)
    assert (intent["state"], intent["source"], intent["workflow_event_id"]) == ("CONFIRMED", "HUMAN_APPLIED", event["id"])


def test_applied_then_later_status_is_idempotent_and_weak_identity_is_skipped(conn):
    ws = _ws(conn)
    mark_applied(conn, ws)
    record_status_change(conn, workspace_id=ws, new_status="interview", effective_date="2026-09-25")
    weak = _ws(conn, {"company": "Weak Co", "title": "Eng", "location": "UK"})
    mark_applied(conn, weak)
    assert conn.execute("SELECT COUNT(*) FROM submission_intents").fetchone()[0] == 1


def test_human_applied_while_autonomous_claim_is_live_confirms_it_and_blocks_the_click(conn, settings, seeded):
    # Spec §10.2: a human submission must permanently suppress autonomous
    # submission for the identity. If an autonomous attempt holds the live
    # CLAIMED intent, the human confirmation turns it CONFIRMED, the attempt
    # may not dispatch, and its expiry never releases the intent.
    from tests.webapp.services.test_autonomy_preclick import IDENT, T, click, submit_grant
    from webapp.persistence.autonomy_ledger import record_human_intent
    from webapp.services.autonomy import expire_unclicked, record_click_dispatched
    attempt = click(conn, settings, seeded, submit_grant(conn, settings, seeded)).attempt_id
    record_human_intent(conn, workspace_id=seeded, account_id=ACCOUNT, source="HUMAN_APPLIED", now=T)
    conn.commit()
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key=IDENT)["state"] == "CONFIRMED"
    assert record_click_dispatched(conn, attempt_id=attempt, now=T + timedelta(seconds=1)) is False
    assert attempt_state(conn, attempt) == "EXPIRED_UNCLICKED"
    assert expire_unclicked(conn, now=T + timedelta(minutes=5)) == 0
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key=IDENT)["state"] == "CONFIRMED"
    assert conn.execute("SELECT COUNT(*) FROM limit_reservations WHERE status = 'RESERVED'").fetchone()[0] == 0


def test_backfill_covers_applications_applied_before_6b(conn):
    ws = _ws(conn)
    conn.execute(
        "INSERT INTO workflow_events (id, workspace_id, previous_status, new_status, effective_date, created_at) "
        "VALUES ('evt_old', ?, NULL, 'applied', '2026-08-01', '2026-08-01T00:00:00+00:00')", (ws,))
    conn.execute(f"DELETE FROM schema_migrations WHERE id = '{BACKFILL}'")
    conn.commit()
    apply_migrations(conn)
    key, _, _ = workspace_identity(conn, ws)
    intent = live_intent(conn, account_id=ACCOUNT, job_identity_key=key)
    assert (intent["state"], intent["workflow_event_id"]) == ("CONFIRMED", "evt_old")
    conn.execute(f"DELETE FROM schema_migrations WHERE id = '{BACKFILL}'")
    conn.commit()
    apply_migrations(conn)  # re-running never duplicates
    assert conn.execute("SELECT COUNT(*) FROM submission_intents").fetchone()[0] == 1


def test_backfill_covers_handoff_confirmations(conn):
    ws = _ws(conn)
    add_handoff_confirmation(conn, ws)
    conn.execute(f"DELETE FROM schema_migrations WHERE id = '{BACKFILL}'")
    conn.commit()
    apply_migrations(conn)
    key, _, _ = workspace_identity(conn, ws)
    assert live_intent(conn, account_id=ACCOUNT, job_identity_key=key)["source"] == "HUMAN_HANDOFF"


def test_backfill_on_a_representative_pre_6b_database(tmp_path, monkeypatch):
    """A database created and populated before Bundle 6B: no autonomy tables,
    applied workspaces recorded through the real flow, handoff confirmations,
    a weak-identity application, and one job confirmed both ways. Migrations
    016 then 017 must produce exactly one CONFIRMED intent per strong
    identity, and re-running all migrations is a no-op."""
    db = tmp_path / "pre6b.sqlite3"
    noop = lambda conn: None  # noqa: E731
    monkeypatch.setattr(migrations_module, "_migrate_autonomy_contract", noop)
    monkeypatch.setattr(migrations_module, "_migrate_autonomy_human_intent_backfill", noop)
    monkeypatch.setattr(workflow_module, "record_human_intent", lambda *a, **k: None)  # pre-6B: no hook
    init_db(db)
    c = connect(db)
    try:
        c.execute("DELETE FROM schema_migrations WHERE id IN ('016_autonomy_contract', ?)", (BACKFILL,))
        c.commit()
        assert c.execute("SELECT name FROM sqlite_master WHERE name = 'submission_intents'").fetchone() is None
        maker = lambda cn: create_workspace(cn, company="Acme", title="Eng")["id"]  # noqa: E731
        applied = _ws(c, maker=maker)
        mark_applied(c, applied)
        both = _ws(c, {**RECORD, "source_record_id": "78"}, maker=maker)
        mark_applied(c, both)
        add_handoff_confirmation(c, both, suffix="2")
        handoff_only = _ws(c, {**RECORD, "source_record_id": "79"}, maker=maker)
        add_handoff_confirmation(c, handoff_only, suffix="3")
        weak = _ws(c, {"company": "Weak Co", "title": "Eng", "location": "UK"}, maker=maker)
        mark_applied(c, weak)
        monkeypatch.undo()
        apply_migrations(c)
        rows = c.execute("SELECT job_identity_key, state, source FROM submission_intents ORDER BY seq").fetchall()
        assert [(r["state"], r["source"]) for r in rows] == [
            ("CONFIRMED", "HUMAN_APPLIED"), ("CONFIRMED", "HUMAN_APPLIED"), ("CONFIRMED", "HUMAN_HANDOFF")]
        assert {r["job_identity_key"] for r in rows} == {workspace_identity(c, w)[0] for w in (applied, both, handoff_only)}
        apply_migrations(c)
        init_db(db)
        assert c.execute("SELECT COUNT(*) FROM submission_intents").fetchone()[0] == 3
    finally:
        c.close()

