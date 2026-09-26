from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.persistence import autonomy_prepare as ap
from webapp.persistence.db import connect
from tests.webapp.services.autonomy_6c_fixtures import ACCOUNT, NOW, enable_prepare, make_workspace


@pytest.fixture
def client(tmp_path):
    from webapp.config import Settings
    settings = Settings(db_path=tmp_path / "db.sqlite3")
    with TestClient(create_app(settings)) as c:
        c.settings = settings
        yield c


def _conn(client):
    return connect(client.settings.db_path)


def test_enrol_unenrol_and_ownership(client):
    conn = _conn(client)
    ws = make_workspace(conn)
    conn.close()
    assert client.post(f"/api/workspaces/{ws}/autonomy/enrol").status_code == 200
    conn = _conn(client)
    assert ap.is_enrolled(conn, ws)
    conn.close()
    assert client.post(f"/api/workspaces/{ws}/autonomy/unenrol").status_code == 200
    assert client.post("/api/workspaces/ws_nope/autonomy/enrol").status_code == 404


def test_review_pack_needs_a_current_revision(client):
    conn = _conn(client)
    ws = make_workspace(conn)
    conn.close()
    assert client.post(f"/api/workspaces/{ws}/autonomy/review-pack").status_code == 409
    assert client.post("/api/workspaces/ws_nope/autonomy/review-pack").status_code == 404


def test_retry_refuses_non_eligible_failures(client):
    conn = _conn(client)
    ws = make_workspace(conn)
    conn.close()
    r = client.post("/api/autonomy/retry", json={"subject_type": "APPLICATION", "subject_id": ws,
                                                 "step_kind": "FIT"})
    assert r.status_code == 409
    assert client.post("/api/autonomy/retry", json={"subject_type": "APPLICATION", "subject_id": "ws_nope",
                                                    "step_kind": "FIT"}).status_code == 404
    assert client.post("/api/autonomy/retry", json={"subject_type": "NOPE", "subject_id": ws,
                                                    "step_kind": "FIT"}).status_code == 422


def test_candidate_exception_resolution_routes(client):
    from tests.webapp.services.autonomy_6c_fixtures import make_screening_row
    conn = _conn(client)
    screening = make_screening_row(conn, candidate_id="cand_api", outcome="REQUIRE_USER", could_unlock=True)
    exc = ap.open_candidate_exception(conn, account_id=ACCOUNT, search_workspace_id="search_default",
                                      candidate_id="cand_api", screening_id=screening["id"], items=[], now=NOW)
    conn.commit()
    conn.close()
    assert client.post("/api/autonomy/candidate-exceptions/nope/resolve",
                       json={"resolution": "DISMISS"}).status_code == 404
    assert client.post(f"/api/autonomy/candidate-exceptions/{exc['id']}/resolve",
                       json={"resolution": "MAYBE"}).status_code == 422
    # the candidate row does not exist, so a Promote is refused, not a server error
    assert client.post(f"/api/autonomy/candidate-exceptions/{exc['id']}/resolve",
                       json={"resolution": "PROMOTE"}).status_code in (404, 409)


def test_inbox_views_mark_seen_but_actionable_badge_stays(client):
    from webapp.services.autonomy_inbox import notify_outcome
    conn = _conn(client)
    ws = make_workspace(conn)
    ap.enqueue_application(conn, application_workspace_id=ws, account_id=ACCOUNT, now=NOW)
    ap.set_dormant(conn, queue="APPLICATION", item_id=ws, now=NOW)
    notify_outcome(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=ws, kind="NEEDS_USER",
                   reason="pack_review", fingerprint="f", detail={}, now=NOW)
    notify_outcome(conn, account_id=ACCOUNT, subject_type="APPLICATION", subject_id=ws, kind="PREPARED",
                   reason="prepared", fingerprint="f", detail={}, now=NOW)
    conn.commit()
    conn.close()
    assert client.get("/api/autonomy/inbox/summary").json() == {"badge": 2, "actionable": 1,
                                                                "informational_unseen": 1}
    body = client.get("/api/autonomy/inbox").json()
    assert len(body["needs_answer"]) == 1 and len(body["ready"]) == 1
    assert client.get("/api/autonomy/inbox/summary").json()["badge"] == 1
    assert client.get("/autonomy/inbox").status_code == 200


def test_autonomy_status_reports_the_scheduler(client):
    body = client.get("/api/autonomy").json()
    assert body["scheduler_enabled"] is False and body["driver_running"] is False and "last_tick_at" in body


def test_dossier_has_pack_detail_system_items_and_labelled_sections(ready_chain):
    from webapp.services.autonomy_dossier import build_dossier
    from webapp.services.autonomy_prepare import enrol, system_gate4
    from webapp.services.autonomy_prepare_auth import authorize_prepare
    from webapp.services.autonomy_prepare import prepare_snapshot
    conn, ws, settings = ready_chain
    enable_prepare(conn)
    s = dataclasses.replace(settings, autonomy_max_capability="PREPARE", autonomy_scheduler_enabled=True)
    enrol(conn, account_id=ACCOUNT, application_workspace_id=ws, actor="u", now=NOW)
    auth = authorize_prepare(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws, now=NOW)
    _, detail = prepare_snapshot(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws)
    system_gate4(conn, settings=s, account_id=ACCOUNT, application_workspace_id=ws, authorization=auth,
                 expected_revision=detail["pack_revision"], now=NOW)
    dossier = build_dossier(conn, account_id=ACCOUNT, application_workspace_id=ws, settings=s)
    pack = dossier["pack"]
    assert pack["content_hash"].startswith("sha256:") and pack["pack_revision"] and set(pack["source_content_ids"]) == {
        "profile_snapshot", "job_posting_snapshot", "job_fit_result", "application_intelligence_result"}
    assert pack["system_confirmed"] is True
    assert dossier["current_state_derived"]["next"] == "PREPARED"
    assert "attempt_history_observational" in dossier and dossier["enrolments"][0]["action"] == "ENROL"
    assert isinstance(dossier["system_review"], list)
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("webapp/templates"), autoescape=True)
    html = env.get_template("autonomy_dossier.html").render(dossier=dossier, request=None)
    assert "system-confirmed" in html and pack["content_hash"] in html


from tests.webapp.services.autonomy_6c_fixtures import ready_chain  # noqa: E402,F401


def test_dossier_page_labels_derived_state_and_observational_history(client):
    conn = _conn(client)
    ws = make_workspace(conn)
    conn.close()
    html = client.get(f"/workspaces/{ws}/autonomy").text
    for marker in ('data-dossier-section="current-state"', 'data-dossier-section="pack"',
                   'data-dossier-section="system-review"', 'data-dossier-section="attempt-history"',
                   "Current state (derived)", "Attempt history (observational)",
                   "data-autonomy-enrol", "data-autonomy-unenrol", "data-autonomy-review-pack"):
        assert marker in html, marker
