"""6E-A persistence (spec §17): round-trips, one authorization per run,
append-only, no cleartext at rest; and the existing record_human_intent
keeps exactly one live intent when a HUMAN_AUTHORIZED intent is live."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from webapp.persistence import submit as s
from webapp.persistence.db import connect, init_db
from webapp.persistence.fill import CleartextAtRestError
from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID

pytestmark = pytest.mark.sqlite_only  # turns SQLite foreign keys off to test row shape alone


NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
ACC = "account_local"


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "db.sqlite3")
    c = connect(tmp_path / "db.sqlite3")
    c.execute("PRAGMA foreign_keys = OFF")  # persistence shape only; FKs are proven by the migration test
    yield c
    c.close()


def _auth(conn, run="fr_1", grant="gr_1", id_=None):
    return s.insert_authorization(conn, id=id_, account_id=ACC, application_workspace_id="ws_1", fill_run_id=run,
                                  review_hash="sha256:" + "a" * 64, review={"schema": "submission-review"},
                                  grant_id=grant, actor="u", now=NOW)


def test_authorization_round_trip_and_one_per_run(conn):
    a = _auth(conn, id_="hsa_fixed")
    assert a["id"] == "hsa_fixed" and a["review"] == {"schema": "submission-review"}
    assert s.authorization_for_run(conn, "fr_1")["id"] == "hsa_fixed"
    assert s.get_authorization(conn, "hsa_fixed")["grant_id"] == "gr_1"
    assert s.authorization_for_grant(conn, "gr_1")["id"] == "hsa_fixed"
    with pytest.raises(sqlite3.IntegrityError):
        _auth(conn, grant="gr_2")
    assert s.authorization_for_run(conn, "fr_none") is None


def test_reobservation_requests_latest(conn):
    s.insert_reobservation_request(conn, account_id=ACC, fill_run_id="fr_1", now=NOW)
    later = s.insert_reobservation_request(conn, account_id=ACC, fill_run_id="fr_1",
                                           now=NOW.replace(minute=1))
    assert s.latest_reobservation_request(conn, "fr_1")["id"] == later["id"]


def test_submit_observations_latest_by_phase_and_no_cleartext(conn):
    for phase in ("REVIEW", "PRE_SUBMIT", "REVIEW"):
        last = s.insert_submit_observation(conn, account_id=ACC, application_workspace_id="ws_1", fill_run_id="fr_1",
                                           attempt_id=None, phase=phase, structure_fingerprint="s",
                                           observation_fingerprint="o", observation={"elements": []}, now=NOW)
    assert s.latest_submit_observation(conn, "fr_1", "REVIEW")["id"] == last["id"]
    assert s.latest_submit_observation(conn, "fr_1", "POST_SUBMIT") is None
    with pytest.raises(CleartextAtRestError):
        s.insert_submit_observation(conn, account_id=ACC, application_workspace_id="ws_1", fill_run_id="fr_1",
                                    attempt_id=None, phase="REVIEW", structure_fingerprint="s",
                                    observation_fingerprint="o", observation={"value": "1 month"}, now=NOW)


def test_events_results_and_append_only(conn):
    a = _auth(conn)
    e = s.append_submit_event(conn, authorization_id=a["id"], attempt_id="att_1", event="CLICK_PERFORMED",
                              detail={"at_ms": 5}, now=NOW)
    assert [x["event"] for x in s.submit_events(conn, a["id"])] == ["CLICK_PERFORMED"]
    assert e["detail"] == {"at_ms": 5}
    r = s.insert_submission_result(conn, attempt_id="att_1", result={"state": "CONFIRMED_SUCCESS"},
                                   result_hash="sha256:" + "b" * 64, now=NOW)
    assert s.get_submission_result(conn, "att_1")["result"] == {"state": "CONFIRMED_SUCCESS"} and r["id"]
    with pytest.raises(sqlite3.IntegrityError):
        s.insert_submission_result(conn, attempt_id="att_1", result={}, result_hash="x", now=NOW)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE submit_events SET event = 'RESULT_REPORTED'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM human_submit_authorizations")
    with pytest.raises(CleartextAtRestError):
        s.append_submit_event(conn, authorization_id=a["id"], attempt_id=None, event="SIGNAL_OBSERVED",
                              detail={"rendered_value": "x"}, now=NOW)


def test_record_human_intent_keeps_one_live_intent_when_human_authorized_is_live(tmp_path):
    from webapp.persistence.application_identity import save_application_identity
    from webapp.persistence.autonomy_ledger import claim_intent, record_human_intent, workspace_identity
    from webapp.persistence.workspaces import create_workspace
    init_db(tmp_path / "i.sqlite3")
    c = connect(tmp_path / "i.sqlite3")
    ws = create_workspace(c, company="Acme", title="Engineer", account_id=DEFAULT_ACCOUNT_ID)["id"]
    save_application_identity(c, application_workspace_id=ws, source_record={
        "source": "greenhouse", "source_record_id": "123", "source_url": "http://127.0.0.1:8430/acme/jobs/123",
        "company": "Acme", "title": "Engineer", "location": "London"})
    key, _, _ = workspace_identity(c, ws)
    live = claim_intent(c, account_id=ACC, job_identity_key=key, application_workspace_id=ws,
                        source="HUMAN_AUTHORIZED", now=NOW)
    out = record_human_intent(c, workspace_id=ws, account_id=ACC, source="HUMAN_APPLIED", now=NOW)
    assert out["id"] == live["id"]
    rows = c.execute("SELECT source, state FROM submission_intents").fetchall()
    assert [tuple(r) for r in rows] == [("HUMAN_AUTHORIZED", "CONFIRMED")]
    c.close()
