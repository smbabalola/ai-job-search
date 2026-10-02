"""Bundle 7 spec §20.4 (Task 28): the guarded purge. Account A's full graph
is purged next to account B's: DELETE-class rows go, PSEUDONYMIZE columns are
rewritten, RETAIN rows stay tagged, B is untouched, the object-store prefix is
empty, a rerun is a no-op, append-only history stays protected outside the
purge, and the email can sign up again."""
from __future__ import annotations

import dataclasses
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from product.retention_policy import load_retention_policy
from webapp.app import create_app
from webapp.persistence import dbapi
from webapp.persistence.db import connect
from webapp.persistence.tenancy import TENANT_TABLES
from webapp.services.account_lifecycle import request_deletion
from webapp.services.ownership import AccountScope
from webapp.services.purge import account_filter, expire_retained, purge_account, purge_sequence
from webapp.storage.object_store import object_store_from_settings
from tests.webapp.admin_helpers import admin_settings
from tests.webapp.auth_helpers import PASSWORD, publish_legal_documents, sign_in, sign_up, sign_up_and_verify
from tests.webapp.factories import build_account_graph

NOW = datetime.now(timezone.utc)


def _signed_up(app, email, name):
    client = TestClient(app)
    sign_up_and_verify(client, email=email, name=name)
    sign_in(client, email=email)
    me = client.get("/auth/me").json()
    return client, me["account_id"], me["user_id"] if "user_id" in me else None


@pytest.fixture
def world(tmp_path):
    settings = admin_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app):
        publish_legal_documents(settings)
        _, a, _ = _signed_up(app, "ada@example.com", "Ada Lovelace")
        _, b, _ = _signed_up(app, "grace@example.com", "Grace Hopper")
        conn = connect(settings)
        try:
            for account_id in (a, b):
                build_account_graph(conn, account_id=account_id, documents_root=settings.documents_root)
                conn.execute("INSERT INTO billing_customers (account_id, provider, provider_customer_id, created_at) "
                             "VALUES (?, 'fake', ?, ?)", (account_id, f"cus_{account_id}", NOW.isoformat()))
            conn.commit()
        finally:
            conn.close()
        yield SimpleNamespace(app=app, settings=settings, a=a, b=b)


def _users(conn, account_id):
    return [r[0] for r in conn.execute("SELECT user_id FROM account_memberships WHERE account_id = ?", (account_id,))]


def _hash(row) -> str:
    return hashlib.sha256(repr(tuple(str(v) for v in tuple(row))).encode()).hexdigest()


def _snapshot(conn, account_id, user_ids) -> dict[str, set[str]]:
    out = {}
    for table in purge_sequence(conn):
        where, params = account_filter(table, account_id, user_ids)
        out[table] = {_hash(r) for r in conn.execute(f'SELECT * FROM "{table}" WHERE {where}', tuple(params))}
    return out


def _all(conn, table) -> set[str]:
    return {_hash(r) for r in conn.execute(f'SELECT * FROM "{table}"')}


def _request_deletion(world, *, cooling_off=timedelta(0)):
    policy = dataclasses.replace(load_retention_policy("product/policies/retention-policy.dev.json"),
                                 deletion_cooling_off=cooling_off)
    conn = connect(world.settings)
    try:
        user_id = _users(conn, world.a)[0]
        scope = AccountScope(account_id=world.a, profile_root=world.settings.profile_root, user_id=user_id)
        request_deletion(conn, scope, password=PASSWORD, typed_email="ada@example.com", now=NOW,
                         settings=world.settings, policy=policy)
    finally:
        conn.close()


def _purge(world, now=None):
    return purge_account(lambda: connect(world.settings), account_id=world.a,
                         object_store=object_store_from_settings(world.settings), now=now or NOW,
                         settings=world.settings)


def test_the_purge_removes_a_and_leaves_b_untouched(world):
    _request_deletion(world)
    conn = connect(world.settings)
    try:
        a_users, b_users = _users(conn, world.a), _users(conn, world.b)
        before_a, before_b = _snapshot(conn, world.a, a_users), _snapshot(conn, world.b, b_users)
        store = object_store_from_settings(world.settings)
        assert any(k for k in _keys(world.settings, world.a))  # A had documents
    finally:
        conn.close()
    report = _purge(world)
    assert report.status == "purged" and report.deleted
    conn = connect(world.settings)
    try:
        for table, rows in before_a.items():
            spec = TENANT_TABLES[table]
            after = _all(conn, table)
            if spec.purge == "DELETE":
                assert not (rows & after), f"{table}: A rows survived the purge"
            elif spec.purge == "RETAIN" and table != "account_deletions":  # the purge stamps completed_at
                assert rows <= after, f"{table}: retained rows were lost"
        for table, rows in before_b.items():
            if table in ("accounts", "users"):
                continue
            assert rows <= _all(conn, table), f"{table}: a B row changed"
        user = conn.execute("SELECT email_normalized, email_display, display_name, status FROM users WHERE id = ?",
                            (a_users[0],)).fetchone()
        assert user[0] == f"purged:{a_users[0]}:" + hashlib.sha256(b"ada@example.com").hexdigest()
        assert (user[1], user[2], user[3]) == (f"deleted-{a_users[0]}@invalid", "Deleted user", "PURGED")
        account = conn.execute("SELECT display_name, status FROM accounts WHERE id = ?", (world.a,)).fetchone()
        assert tuple(account) == ("Deleted account", "PURGED")
        b_user = conn.execute("SELECT email_normalized, status FROM users WHERE id = ?", (b_users[0],)).fetchone()
        assert tuple(b_user) == ("grace@example.com", "ACTIVE")
        tags = {r[0]: r[1] for r in conn.execute("SELECT table_name, retain_class FROM purge_retention_tags "
                                                 "WHERE account_id = ?", (world.a,))}
        assert tags["audit_log"] == "SECURITY_AUDIT" and tags["billing_customers"] == "BILLING_FINANCIAL"
        assert conn.execute("SELECT COUNT(*) FROM purge_in_progress").fetchone()[0] == 0
        assert conn.execute("SELECT completed_at FROM account_deletions WHERE account_id = ?",
                            (world.a,)).fetchone()[0] is not None
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE account_id = ?", (world.a,))]
        assert "ACCOUNT_PURGED" in actions
        mail = conn.execute("SELECT to_address, account_id FROM outbound_messages WHERE template_id = "
                            "'account.deletion_completed'").fetchall()
        assert [tuple(m) for m in mail] == [("ada@example.com", None)]
    finally:
        conn.close()
    assert _keys(world.settings, world.a) == [] and _keys(world.settings, world.b)
    assert _purge(world).status == "already_purged"


def _keys(settings, account_id):
    from pathlib import Path
    root = Path(settings.documents_root) / "document_blobs" / "accounts" / account_id
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_the_purge_waits_for_the_cooling_off_and_a_canceled_request(world):
    _request_deletion(world, cooling_off=timedelta(days=7))
    assert _purge(world).status == "not_due"
    assert _purge(world, now=NOW + timedelta(days=8)).status == "purged"


def test_an_account_that_did_not_ask_is_never_purged(world):
    assert _purge(world).status == "not_requested"


def test_append_only_history_is_protected_outside_the_purge(world):
    conn = connect(world.settings)
    try:
        with pytest.raises(dbapi.IntegrityError if conn.dialect == "postgres" else Exception):
            conn.execute("DELETE FROM audit_log")
        conn.rollback()
        with pytest.raises(Exception):
            conn.execute("UPDATE audit_log SET action = 'LOGOUT'")
        conn.rollback()
    finally:
        conn.close()


def test_the_guard_of_a_running_purge_is_invisible_to_other_transactions(world):
    purging, other = connect(world.settings), connect(world.settings)
    try:
        if other.dialect == "sqlite":
            other.execute("PRAGMA busy_timeout = 200")  # SQLite serializes writers: the other one waits, then fails
        with dbapi.account_transaction(purging, world.a):
            purging.execute("INSERT INTO purge_in_progress (account_id, started_at) VALUES (?, ?)",
                            (world.a, NOW.isoformat()))
            with pytest.raises(Exception):
                other.execute("DELETE FROM audit_log WHERE account_id = ?", (world.b,))
            if other.in_transaction:
                other.rollback()
            purging.execute("DELETE FROM purge_in_progress")
        assert other.execute("SELECT COUNT(*) FROM audit_log WHERE account_id = ?", (world.b,)).fetchone()[0] > 0
    finally:
        purging.close()
        other.close()


def test_the_email_can_sign_up_again_after_the_purge(world):
    _request_deletion(world)
    _purge(world)
    client = TestClient(world.app)
    sign_up(client, email="ada@example.com", name="Ada Again")
    conn = connect(world.settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE email_normalized = 'ada@example.com'").fetchone()[0] == 1
    finally:
        conn.close()


def test_retained_rows_expire_after_their_period_only(world):
    _request_deletion(world)
    _purge(world)
    policy = load_retention_policy("product/policies/retention-policy.dev.json")  # SECURITY_AUDIT 365 days
    conn = connect(world.settings)
    try:
        assert expire_retained(conn, policy=policy, now=NOW + timedelta(days=30)) == 0
        expired = expire_retained(conn, policy=policy, now=NOW + timedelta(days=400))
        assert expired >= 1
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE account_id = ?", (world.a,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM billing_customers WHERE account_id = ?", (world.a,)).fetchone()[0] == 1
        production = load_retention_policy("product/policies/retention-policy.v1.json")  # null periods: kept
        assert expire_retained(conn, policy=production, now=NOW + timedelta(days=99999)) == 0
    finally:
        conn.close()


def test_the_same_email_can_be_purged_twice(world):
    """A purged address may sign up again (pseudonymized email); deleting that
    second account must purge too, not collide on the tombstone."""
    _request_deletion(world)
    assert _purge(world).status == "purged"
    _, again, _ = _signed_up(world.app, "ada@example.com", "Ada Again")
    first = world.a
    world.a = again
    _request_deletion(world)
    assert _purge(world).status == "purged"
    conn = connect(world.settings)
    try:
        tombstones = [r[0] for r in conn.execute("SELECT email_normalized FROM users WHERE status = 'PURGED'")]
        statuses = {r[0]: r[1] for r in conn.execute("SELECT id, status FROM accounts WHERE id IN (?, ?)",
                                                      (first, again))}
    finally:
        conn.close()
    assert len(tombstones) == len(set(tombstones)) == 2
    assert set(statuses.values()) == {"PURGED"}


def test_a_cancel_that_lands_during_the_purge_wins_and_keeps_the_documents(world, monkeypatch):
    """The purge re-checks the request under the account lock; nothing is deleted
    (rows or objects) for a deletion the user cancelled meanwhile."""
    from webapp.services import purge as purge_module
    from webapp.services.account_lifecycle import cancel_deletion
    _request_deletion(world)
    real_sequence = purge_module.purge_sequence

    def cancel_then_order(conn):
        other = connect(world.settings)
        try:
            user_id = _users(other, world.a)[0]
            scope = AccountScope(account_id=world.a, profile_root=world.settings.profile_root, user_id=user_id)
            cancel_deletion(other, scope, now=NOW, settings=world.settings)
        finally:
            other.close()
        return real_sequence(conn)

    monkeypatch.setattr(purge_module, "purge_sequence", cancel_then_order)
    keys_before = _keys(world.settings, world.a)
    report = _purge(world)
    assert report.status == "not_requested"
    conn = connect(world.settings)
    try:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (world.a,)).fetchone()[0] == "ACTIVE"
    finally:
        conn.close()
    assert keys_before and _keys(world.settings, world.a) == keys_before
