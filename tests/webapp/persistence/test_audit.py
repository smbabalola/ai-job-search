"""Bundle 7 spec §20.1: append-only audit log with a closed vocabulary."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from product.audit_actions import ACTOR_TYPES, AUDIT_ACTIONS
from webapp.persistence import dbapi
from webapp.persistence.audit import audit, ip_hash
from webapp.persistence.db import connect, init_db

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    connection = connect(path)
    yield connection
    connection.close()


def test_vocabulary_is_the_spec_list():
    assert {"LOGIN_SUCCEEDED", "LOGIN_FAILED", "LOGOUT", "SESSIONS_REVOKED", "PASSWORD_CHANGED", "PASSWORD_RESET",
            "EMAIL_VERIFIED", "EMAIL_CHANGED", "EXTENSION_DEVICE_PAIRED", "EXTENSION_DEVICE_REVOKED",
            "EXTENSION_TOKEN_REUSE", "PLAN_CHECKOUT_STARTED", "SUBSCRIPTION_STATE_CHANGED",
            "ENTITLEMENT_GRANT_CREATED", "ENTITLEMENT_GRANT_REVOKED", "ACCOUNT_SUSPENDED", "ACCOUNT_UNSUSPENDED",
            "ACCOUNT_DELETION_REQUESTED", "ACCOUNT_DELETION_CANCELED", "ACCOUNT_PURGED", "DATA_EXPORT_REQUESTED",
            "DATA_EXPORT_DOWNLOADED", "PLATFORM_CONTROL_SET", "STAFF_ROLE_CHANGED", "ANNOUNCEMENT_PUBLISHED",
            "ADMIN_ACCOUNT_VIEWED", "DEAD_LETTER_RETRIED", "CONSENT_CHANGED"} <= AUDIT_ACTIONS
    assert ACTOR_TYPES == frozenset({"USER", "STAFF", "EXTENSION_DEVICE", "SYSTEM", "PROVIDER"})


def test_audit_rows_are_recorded_redacted_and_append_only(conn):
    audit(conn, actor_type="USER", actor_id="user_1", account_id="account_local", action="LOGIN_SUCCEEDED",
          ip="203.0.113.7", request_id="req-1", detail={"token": "secret", "method": "password"}, now=NOW,
          secret="k" * 43)
    conn.commit()
    row = conn.execute("SELECT * FROM audit_log").fetchone()
    assert (row["action"], row["actor_type"], row["request_id"]) == ("LOGIN_SUCCEEDED", "USER", "req-1")
    assert row["ip_hash"] == ip_hash("k" * 43, "203.0.113.7") and "203.0.113.7" not in str(tuple(row))
    assert "secret" not in row["detail_json"] and "password" in row["detail_json"]
    with pytest.raises(dbapi.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_log SET action = 'LOGOUT'")
    conn.rollback()
    with pytest.raises(dbapi.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    conn.rollback()


def test_unknown_actions_and_actor_types_are_refused(conn):
    with pytest.raises(ValueError):
        audit(conn, actor_type="USER", actor_id="u", account_id=None, action="MADE_UP", now=NOW)
    with pytest.raises(ValueError):
        audit(conn, actor_type="ROBOT", actor_id="u", account_id=None, action="LOGOUT", now=NOW)


def test_login_success_and_failure_are_audited(tmp_path):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from tests.webapp.auth_helpers import publish_legal_documents, sign_in, sign_up_and_verify
    from webapp.app import create_app
    from webapp.config import Settings

    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[2] / "fixtures" / "extensions",
                        auth_required_in_local=True)
    with TestClient(create_app(settings)) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client, password="wrong password here")
        sign_in(client)
    conn = connect(settings.db_path)
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY seq")]
    failed = conn.execute("SELECT detail_json FROM audit_log WHERE action = 'LOGIN_FAILED'").fetchone()
    conn.close()
    assert actions == ["EMAIL_VERIFIED", "LOGIN_FAILED", "LOGIN_SUCCEEDED"]
    assert "ada@example.com" not in failed["detail_json"]  # only a hash of the email
