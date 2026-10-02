"""Bundle 7 Task 19 (spec §18.1): communication consents are append-only; the
latest row per (user, channel, purpose) wins; no row means not granted."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from webapp.comms.consent import current_consent, record_consent
from webapp.persistence import dbapi, identity
from webapp.persistence.db import connect, init_db
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    created = identity.create_user_with_account(
        conn, email="ada@example.com", password_hash="h", display_name="Ada", legal_document_ids=[], now=NOW,
        profile_store=DatabaseProfileSourceStore())
    conn.commit()
    yield conn, created["account"]["id"], created["user"]["id"]
    conn.close()


def _record(conn, account_id, user_id, state, *, now=NOW):
    record_consent(conn, account_id=account_id, user_id=user_id, channel="EMAIL", purpose="MARKETING", state=state,
                   wording_version="marketing-email.v1", source="settings", now=now)
    conn.commit()


def test_the_default_is_not_granted(world):
    conn, _, user_id = world
    assert current_consent(conn, user_id=user_id, channel="EMAIL", purpose="MARKETING") is False


def test_the_latest_row_wins(world):
    conn, account_id, user_id = world
    _record(conn, account_id, user_id, "GRANTED")
    assert current_consent(conn, user_id=user_id, channel="EMAIL", purpose="MARKETING") is True
    _record(conn, account_id, user_id, "WITHDRAWN", now=NOW + timedelta(minutes=1))
    assert current_consent(conn, user_id=user_id, channel="EMAIL", purpose="MARKETING") is False
    assert conn.execute("SELECT COUNT(*) FROM communication_consents").fetchone()[0] == 2


def test_a_change_is_audited(world):
    conn, account_id, user_id = world
    _record(conn, account_id, user_id, "GRANTED")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log")]
    assert "CONSENT_CHANGED" in actions


def test_consent_rows_are_append_only(world):
    conn, account_id, user_id = world
    _record(conn, account_id, user_id, "GRANTED")
    with pytest.raises(dbapi.IntegrityError, match="append-only"):
        conn.execute("UPDATE communication_consents SET state = 'WITHDRAWN'")
    conn.rollback()


def test_unknown_values_are_refused(world):
    conn, account_id, user_id = world
    with pytest.raises(ValueError):
        record_consent(conn, account_id=account_id, user_id=user_id, channel="EMAIL", purpose="MARKETING",
                       state="MAYBE", wording_version="v", source="settings", now=NOW)
