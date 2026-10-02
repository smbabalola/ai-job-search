"""Bundle 7 spec A4: server-side opaque sessions with idle and absolute expiry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from webapp.persistence import identity
from webapp.persistence.db import connect, init_db
from webapp.services.sessions import SessionService
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
SECRET = "s" * 43


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    user = identity.create_user_with_account(
        conn, email="ada@example.com", password_hash="h", display_name="Ada", legal_document_ids=[],
        now=NOW, profile_store=DatabaseProfileSourceStore())["user"]
    conn.commit()
    yield conn, user, SessionService(SECRET)
    conn.close()


def test_session_ids_are_stored_hashed_and_resolve(world):
    conn, user, sessions = world
    raw, csrf = sessions.create(conn, user_id=user["id"], kind="CUSTOMER", now=NOW, ip="203.0.113.9",
                                user_agent="Mozilla/5.0 (Windows NT 10.0) Firefox/130.0")
    conn.commit()
    assert raw not in str([tuple(r) for r in conn.execute("SELECT * FROM web_sessions")])
    assert "203.0.113.9" not in str([tuple(r) for r in conn.execute("SELECT * FROM web_sessions")])
    resolved = sessions.resolve(conn, raw, now=NOW + timedelta(minutes=5))
    assert resolved["user_id"] == user["id"] and resolved["kind"] == "CUSTOMER"
    assert csrf == sessions.csrf_token(raw) and csrf != sessions.csrf_token("other")
    assert sessions.resolve(conn, "not-a-session", now=NOW) is None


def test_customer_idle_expiry_is_seven_days(world):
    conn, user, sessions = world
    raw, _ = sessions.create(conn, user_id=user["id"], kind="CUSTOMER", now=NOW, ip=None, user_agent=None)
    assert sessions.resolve(conn, raw, now=NOW + timedelta(days=7) - timedelta(seconds=1)) is not None
    raw2, _ = sessions.create(conn, user_id=user["id"], kind="CUSTOMER", now=NOW, ip=None, user_agent=None)
    assert sessions.resolve(conn, raw2, now=NOW + timedelta(days=7, seconds=1)) is None


def test_activity_extends_idle_but_never_past_the_absolute_thirty_days(world):
    conn, user, sessions = world
    raw, _ = sessions.create(conn, user_id=user["id"], kind="CUSTOMER", now=NOW, ip=None, user_agent=None)
    moment = NOW
    for _ in range(6):  # a visit every five days keeps it alive...
        moment += timedelta(days=5)
        assert sessions.resolve(conn, raw, now=moment) is not None
    assert sessions.resolve(conn, raw, now=NOW + timedelta(days=30, seconds=1)) is None  # ...until 30 days


def test_last_seen_is_written_at_most_once_a_minute(world):
    conn, user, sessions = world
    raw, _ = sessions.create(conn, user_id=user["id"], kind="CUSTOMER", now=NOW, ip=None, user_agent=None)
    sessions.resolve(conn, raw, now=NOW + timedelta(seconds=30))
    assert conn.execute("SELECT last_seen_at FROM web_sessions").fetchone()["last_seen_at"] == NOW.isoformat()
    sessions.resolve(conn, raw, now=NOW + timedelta(seconds=61))
    assert conn.execute("SELECT last_seen_at FROM web_sessions").fetchone()["last_seen_at"] != NOW.isoformat()


def test_staff_sessions_idle_out_after_thirty_minutes_and_end_at_eight_hours(world):
    conn, user, sessions = world
    raw, _ = sessions.create(conn, user_id=user["id"], kind="STAFF", now=NOW, ip=None, user_agent=None)
    assert sessions.resolve(conn, raw, now=NOW + timedelta(minutes=29)) is not None
    assert sessions.resolve(conn, raw, now=NOW + timedelta(minutes=29 + 31)) is None
    raw2, _ = sessions.create(conn, user_id=user["id"], kind="STAFF", now=NOW, ip=None, user_agent=None)
    moment = NOW
    while moment < NOW + timedelta(hours=8) - timedelta(minutes=20):
        moment += timedelta(minutes=20)
        assert sessions.resolve(conn, raw2, now=moment) is not None
    assert sessions.resolve(conn, raw2, now=NOW + timedelta(hours=8, seconds=1)) is None


def test_revoke_and_revoke_all_except_current(world):
    conn, user, sessions = world
    keep, _ = sessions.create(conn, user_id=user["id"], kind="CUSTOMER", now=NOW, ip=None, user_agent=None)
    drop1, _ = sessions.create(conn, user_id=user["id"], kind="CUSTOMER", now=NOW, ip=None, user_agent=None)
    drop2, _ = sessions.create(conn, user_id=user["id"], kind="CUSTOMER", now=NOW, ip=None, user_agent=None)
    assert sessions.revoke_all_for_user(conn, user["id"], reason="PASSWORD_CHANGED", except_session_id=keep) == 2
    assert sessions.resolve(conn, keep, now=NOW) is not None
    assert sessions.resolve(conn, drop1, now=NOW) is None and sessions.resolve(conn, drop2, now=NOW) is None
    sessions.revoke(conn, keep, reason="LOGOUT")
    assert sessions.resolve(conn, keep, now=NOW) is None
