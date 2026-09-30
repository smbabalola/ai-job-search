"""Bundle 7 Task 20 (spec §17.4): the derived Action-required inbox, the
header badge, and the notification and preference routes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.webapp.persistence.autonomy_db import ACCOUNT, NOW, conn, make_workspace  # noqa: F401
from tests.webapp.services.review_fixtures import blocker
from webapp.config import Settings
from webapp.services import inbox
from webapp.services import notifications as n
from webapp.services.ownership import AccountScope

SETTINGS = Settings()


def _scope():
    return AccountScope(account_id=ACCOUNT, profile_root=Path("."), user_id=None)


def _items(conn, kind=None):
    return [i for i in inbox.action_required(conn, _scope(), settings=SETTINGS, now=NOW)
            if kind is None or i.kind == kind]


def test_an_open_blocker_is_listed_until_it_resolves(conn):
    ws = make_workspace(conn)
    created = blocker(conn, ws, "demographic.eeo", "Can you work in the UK?")
    conn.commit()
    items = _items(conn, "blocker")
    assert [(i.subject_id, i.href) for i in items] == [(ws, f"/workspaces/{ws}")]
    conn.execute("UPDATE application_blockers SET status = 'resolved', resolved_at = ? WHERE id = ?",
                 (NOW.isoformat(), created["id"]))
    conn.commit()
    assert _items(conn, "blocker") == []


def test_a_new_blocker_notifies_that_it_needs_an_answer(conn):
    ws = make_workspace(conn)
    blocker(conn, ws, "demographic.eeo")
    conn.commit()
    assert [r[0] for r in conn.execute("SELECT kind FROM notifications")] == ["application.blocker_needs_answer"]


def test_an_ambiguous_submission_is_listed_until_resolved(conn, monkeypatch):
    ws = make_workspace(conn)
    conn.commit()
    status = {"status": "SUBMISSION_UNCLEAR"}
    monkeypatch.setattr(inbox, "submission_status", lambda conn, **kw: dict(status))
    assert [i.subject_id for i in _items(conn, "submission_unclear")] == [ws]
    status["status"] = "SUBMITTED"
    assert _items(conn, "submission_unclear") == []


@pytest.mark.parametrize("status,kind", [("SUBMIT_READY", "fill_awaiting_submit"),
                                         ("CHALLENGE_WAITING", "challenge_handoff")])
def test_fills_awaiting_submit_and_challenges_are_listed(conn, monkeypatch, status, kind):
    ws = make_workspace(conn)
    conn.commit()
    monkeypatch.setattr(inbox, "submission_status", lambda conn, **kw: {"status": status})
    assert [i.subject_id for i in _items(conn, kind)] == [ws]


def test_a_pack_awaiting_review_is_listed(conn, monkeypatch):
    from types import SimpleNamespace
    ws = make_workspace(conn)
    conn.commit()
    monkeypatch.setattr(inbox, "review_state", lambda conn, **kw: SimpleNamespace(state="READY_FOR_REVIEW",
                                                                                   reasons=()))
    assert [i.subject_id for i in _items(conn, "review")] == [ws]
    monkeypatch.setattr(inbox, "review_state", lambda conn, **kw: SimpleNamespace(state="NEEDS_REVIEW",
                                                                                   reasons=("open_deltas",)))
    assert [i.kind for i in _items(conn) if i.subject_id == ws] == ["review_delta"]


def test_6c_questions_are_folded_into_action_required(conn):
    from webapp.persistence import autonomy_prepare as ap
    ws = make_workspace(conn)
    ap.create_notification(conn, account_id=ACCOUNT, key="q1", kind="NEEDS_USER", subject_type="workspace",
                           subject_id=ws, detail={"reason": "x"}, now=NOW)
    conn.commit()
    assert [i.kind for i in _items(conn)] == ["automation_question"]


def test_the_header_badge_counts_action_items_and_unread_critical_notifications(conn):
    ws = make_workspace(conn)
    blocker(conn, ws, "demographic.eeo")  # 1 action item (+ an INFO notification, not counted)
    n.notify(conn, account_id=ACCOUNT, kind="security.token_reuse_detected", subject_type="extension_device",
             subject_id="d", dedupe_key="reuse", detail={}, now=NOW)  # CRITICAL
    conn.commit()
    assert inbox.header_badge(conn, _scope(), settings=SETTINGS, now=NOW) == 2
    note = [x for x in n.list_notifications(conn, ACCOUNT) if x["kind"] == "security.token_reuse_detected"][0]
    n.mark_read(conn, account_id=ACCOUNT, notification_id=note["id"], now=NOW)
    conn.commit()
    assert inbox.header_badge(conn, _scope(), settings=SETTINGS, now=NOW) == 1


# ---- routes -----------------------------------------------------------------------------

@pytest.fixture
def signed_in(tmp_path):
    from tests.webapp.auth_helpers import publish_legal_documents, sign_in, sign_up_and_verify
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        auth_required_in_local=True)
    from webapp.app import create_app
    with TestClient(create_app(settings)) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client)
        yield client, settings, client.get("/auth/me").json()["account_id"]


def _post(client, url, **kwargs):
    from tests.webapp.auth_helpers import csrf_token
    return client.post(url, headers={"X-CSRF-Token": csrf_token(client)}, **kwargs)


def _seed(settings, account_id, key):
    from webapp.persistence.db import connect
    conn = connect(settings)
    n.notify(conn, account_id=account_id, kind="fill.failed", subject_type="workspace", subject_id="ws",
             dedupe_key=key, detail={}, now=datetime.now(timezone.utc))
    conn.commit()
    note = n.list_notifications(conn, account_id)[0]
    conn.close()
    return note


def test_the_notification_routes_list_read_and_archive_own_notifications(signed_in):
    client, settings, account_id = signed_in
    note = _seed(settings, account_id, "mine")
    listed = client.get("/api/notifications").json()["notifications"]
    assert [x["id"] for x in listed] == [note["id"]]
    assert _post(client, f"/api/notifications/{note['id']}/read").status_code == 200
    assert _post(client, f"/api/notifications/{note['id']}/archive").status_code == 200
    assert client.get("/api/notifications").json()["notifications"] == []


def test_another_accounts_notification_is_not_found(signed_in):
    client, settings, _ = signed_in
    from webapp.persistence import identity
    from webapp.persistence.db import connect
    from webapp.storage.profile_sources import DatabaseProfileSourceStore
    conn = connect(settings)
    other = identity.create_user_with_account(
        conn, email="eve@example.com", password_hash="h", display_name="Eve", legal_document_ids=[],
        now=NOW, profile_store=DatabaseProfileSourceStore())["account"]["id"]
    conn.commit()
    conn.close()
    foreign = _seed(settings, other, "theirs")
    assert _post(client, f"/api/notifications/{foreign['id']}/read").status_code == 404
    assert client.get("/api/notifications").json()["notifications"] == []


def test_the_inbox_api_and_page(signed_in):
    client, settings, account_id = signed_in
    _seed(settings, account_id, "k")
    body = client.get("/api/inbox").json()
    assert set(body) == {"action_required", "updates", "announcements", "badge"}
    assert len(body["updates"]) == 1
    page = client.get("/inbox")
    assert page.status_code == 200 and "Action required" in page.text


def test_notification_preferences_round_trip_and_refuse_security(signed_in):
    client, _, _ = signed_in
    assert client.get("/api/notification-preferences").json()["email_modes"]["DISCOVERY"] == "DAILY_DIGEST"
    ok = client.put("/api/notification-preferences", json={"category": "OUTCOME", "mode": "OFF"},
                    headers={"X-CSRF-Token": __import__("tests.webapp.auth_helpers", fromlist=["x"]).csrf_token(client)})
    assert ok.status_code == 200 and ok.json()["email_modes"]["OUTCOME"] == "OFF"
    refused = client.put("/api/notification-preferences", json={"category": "SECURITY", "mode": "OFF"},
                         headers={"X-CSRF-Token": __import__("tests.webapp.auth_helpers",
                                                             fromlist=["x"]).csrf_token(client)})
    assert refused.status_code == 400


def test_the_communications_page_shows_modes_and_the_marketing_opt_in(signed_in):
    client, settings, account_id = signed_in
    page = client.get("/settings/communications")
    assert page.status_code == 200
    assert "always sent" in page.text and "only use this if we start sending product news" in page.text
    saved = _post(client, "/settings/communications", data={"mode_OUTCOME": "DAILY_DIGEST", "marketing_email": "on"},
                  follow_redirects=False)
    assert saved.status_code == 303
    from webapp.comms.consent import current_consent
    from webapp.persistence.db import connect
    conn = connect(settings)
    user_id = conn.execute("SELECT user_id FROM account_memberships WHERE account_id = ?", (account_id,)).fetchone()[0]
    assert current_consent(conn, user_id=user_id, channel="EMAIL", purpose="MARKETING") is True
    assert n.email_modes(conn, account_id)["OUTCOME"] == "DAILY_DIGEST"
    conn.close()
