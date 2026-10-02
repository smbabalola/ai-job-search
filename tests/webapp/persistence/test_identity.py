"""Bundle 7 Task 7: users, memberships, account bootstrap, email tokens (spec §6)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from webapp.persistence import dbapi, identity
from webapp.persistence.db import connect, init_db
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    connection = connect(path)
    connection.execute("INSERT INTO legal_documents (id, kind, version, published_at, content_sha256) "
                       "VALUES ('terms_v1', 'TERMS', '1', '2026-09-01T00:00:00+00:00', ?)", ("a" * 64,))
    connection.execute("INSERT INTO legal_documents (id, kind, version, published_at, content_sha256) "
                       "VALUES ('privacy_v1', 'PRIVACY', '1', '2026-09-01T00:00:00+00:00', ?)", ("b" * 64,))
    connection.commit()
    yield connection
    connection.close()


def _signup(conn, email="Ada@Example.com", name="Ada Lovelace"):
    result = identity.create_user_with_account(
        conn, email=email, password_hash="$argon2id$fake", display_name=name,
        legal_document_ids=["terms_v1", "privacy_v1"], now=NOW, profile_store=DatabaseProfileSourceStore())
    conn.commit()
    return result


def test_normalize_email():
    assert identity.normalize_email(" Foo@Example.COM ") == identity.normalize_email("foo@example.com")
    assert identity.normalize_email("ｆｏｏ@example.com") == "foo@example.com"  # fullwidth "foo"
    for bad in ("", "no-at-sign", "@example.com", "foo@", "a@b@c", "x" * 250 + "@ex.com"):
        with pytest.raises(ValueError):
            identity.normalize_email(bad)


def test_signup_creates_the_whole_account_bootstrap(conn):
    result = _signup(conn)
    user, account = result["user"], result["account"]
    assert user["email_normalized"] == "ada@example.com" and user["email_display"] == "Ada@Example.com"
    assert user["status"] == "PENDING_VERIFICATION" and user["email_verified_at"] is None
    assert (account["kind"], account["status"]) == ("candidate", "ACTIVE")
    membership = conn.execute("SELECT role FROM account_memberships WHERE account_id = ? AND user_id = ?",
                              (account["id"], user["id"])).fetchone()
    assert membership["role"] == "OWNER"
    assert identity.get_owner_account(conn, user["id"])["id"] == account["id"]
    assert conn.execute("SELECT COUNT(*) AS n FROM workspaces WHERE account_id = ? AND kind = 'profile'",
                        (account["id"],)).fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM search_workspaces WHERE account_id = ?",
                        (account["id"],)).fetchone()["n"] == 1
    source = DatabaseProfileSourceStore().for_account(conn, account["id"])
    assert "Ada Lovelace" in source.read(".claude/skills/job-application-assistant/01-candidate-profile.md")
    assert [r["legal_document_id"] for r in conn.execute(
        "SELECT legal_document_id FROM legal_acceptances WHERE user_id = ? ORDER BY legal_document_id",
        (user["id"],))] == ["privacy_v1", "terms_v1"]
    assert identity.consume_email_token(conn, raw=result["verify_token"], purpose="VERIFY_EMAIL", now=NOW) is not None


def test_account_bootstrap_hooks_run_inside_signup(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(identity, "ACCOUNT_BOOTSTRAP_HOOKS",
                        [*identity.ACCOUNT_BOOTSTRAP_HOOKS, lambda c, *, account_id, user_id, now: seen.append(account_id)])
    result = _signup(conn)
    assert seen == [result["account"]["id"]]


def test_equivalent_email_is_a_duplicate(conn):
    _signup(conn, email="Ada@Example.com")
    with pytest.raises(dbapi.IntegrityError):
        _signup(conn, email=" ADA@example.COM ")
    conn.rollback()


def test_unknown_legal_document_is_refused(conn):
    with pytest.raises(dbapi.IntegrityError):
        identity.create_user_with_account(
            conn, email="x@example.com", password_hash="h", display_name="X",
            legal_document_ids=["terms_v9"], now=NOW, profile_store=DatabaseProfileSourceStore())
    conn.rollback()


def test_user_status_and_lookup(conn):
    user = _signup(conn)["user"]
    assert identity.get_user_by_email(conn, "ada@example.com")["id"] == user["id"]
    identity.set_user_status(conn, user["id"], "ACTIVE", now=NOW)
    conn.commit()
    assert identity.get_user(conn, user["id"])["status"] == "ACTIVE"
    with pytest.raises(ValueError):
        identity.set_user_status(conn, user["id"], "BOGUS", now=NOW)


@pytest.mark.parametrize(("purpose", "ttl"), [("VERIFY_EMAIL", timedelta(hours=48)),
                                              ("PASSWORD_RESET", timedelta(hours=1)),
                                              ("EMAIL_CHANGE", timedelta(hours=24))])
def test_email_tokens_are_single_use_purpose_bound_and_expire(conn, purpose, ttl):
    user = _signup(conn)["user"]
    raw = identity.issue_email_token(conn, user_id=user["id"], purpose=purpose, now=NOW,
                                     new_email="new@example.com" if purpose == "EMAIL_CHANGE" else None)
    other = "PASSWORD_RESET" if purpose != "PASSWORD_RESET" else "VERIFY_EMAIL"
    assert identity.consume_email_token(conn, raw=raw, purpose=other, now=NOW) is None
    assert identity.consume_email_token(conn, raw=raw, purpose=purpose, now=NOW + ttl + timedelta(seconds=1)) is None
    token = identity.consume_email_token(conn, raw=raw, purpose=purpose, now=NOW + ttl - timedelta(seconds=1))
    assert token["user_id"] == user["id"]
    if purpose == "EMAIL_CHANGE":
        assert token["new_email_normalized"] == "new@example.com"
    assert identity.consume_email_token(conn, raw=raw, purpose=purpose, now=NOW) is None  # single use
    conn.commit()
    stored = conn.execute("SELECT token_hash FROM email_tokens").fetchall()
    assert all(raw not in r["token_hash"] for r in stored)
