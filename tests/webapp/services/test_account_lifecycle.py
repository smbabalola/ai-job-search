"""Bundle 7 spec O2, §20.4 (Task 28): requesting and cancelling account
deletion, and the restricted page a deletion-requested account lands on."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from product.retention_policy import load_retention_policy
from webapp.app import create_app
from webapp.persistence.db import connect
from webapp.services.account_lifecycle import DeletionRefused, cancel_deletion, request_deletion
from webapp.services.autonomy_controls import kill_switch_state
from webapp.services.ownership import AccountScope
from tests.webapp.admin_helpers import admin_settings
from tests.webapp.auth_helpers import PASSWORD, csrf_token, publish_legal_documents, sign_in, sign_up_and_verify

NOW = datetime.now(timezone.utc)
POLICY = dataclasses.replace(load_retention_policy("product/policies/retention-policy.dev.json"),
                             deletion_cooling_off=timedelta(days=7))


class ProviderSpy:
    def __init__(self):
        self.canceled = []

    def cancel(self, *, subscription_id, immediately):
        self.canceled.append((subscription_id, immediately))


@pytest.fixture
def world(tmp_path):
    settings = admin_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app):
        publish_legal_documents(settings)
        client = TestClient(app)
        sign_up_and_verify(client)
        sign_in(client)
        account_id = client.get("/auth/me").json()["account_id"]
        conn = connect(settings)
        user_id = conn.execute("SELECT user_id FROM account_memberships WHERE account_id = ?",
                               (account_id,)).fetchone()[0]
        conn.execute("INSERT INTO subscriptions (id, account_id, provider, provider_subscription_id, plan_id, interval, "
                     "catalog_version, state, current_period_start, current_period_end, cancel_at_period_end, "
                     "snapshot_json, snapshot_hash, updated_at) VALUES ('sub_1', ?, 'fake', 'psub_1', 'pro', 'month', "
                     "'x', 'ACTIVE', ?, ?, 0, '{}', 'h', ?)",
                     (account_id, NOW.isoformat(), (NOW + timedelta(days=30)).isoformat(), NOW.isoformat()))
        conn.commit()
        scope = AccountScope(account_id=account_id, profile_root=settings.profile_root, user_id=user_id)
        yield SimpleNamespace(app=app, settings=settings, client=client, conn=conn, scope=scope,
                              account_id=account_id, user_id=user_id)
        conn.close()


def _request(world, spy=None, **overrides):
    values = dict(password=PASSWORD, typed_email="ada@example.com")
    values.update(overrides)
    return request_deletion(world.conn, world.scope, now=NOW, settings=world.settings, policy=POLICY,
                            billing_provider=spy, **values)


def test_a_wrong_password_or_email_refuses(world):
    with pytest.raises(DeletionRefused):
        _request(world, password="wrong password here")
    with pytest.raises(DeletionRefused):
        _request(world, typed_email="someone@example.com")
    assert world.conn.execute("SELECT status FROM accounts WHERE id = ?", (world.account_id,)).fetchone()[0] == "ACTIVE"


def test_requesting_deletion_ends_access_and_queues_the_purge(world):
    spy = ProviderSpy()
    _request(world, spy)
    conn = world.conn
    assert conn.execute("SELECT status FROM accounts WHERE id = ?", (world.account_id,)).fetchone()[0] == "DELETION_REQUESTED"
    assert spy.canceled == [("psub_1", True)]  # canceled at once at the provider (DP-7)
    live = conn.execute("SELECT COUNT(*) FROM web_sessions WHERE user_id = ? AND revoked_at IS NULL",
                        (world.user_id,)).fetchone()[0]
    assert live == 1  # only the new deletion-restricted session
    assert kill_switch_state(conn, world.account_id)["engaged"]
    job = conn.execute("SELECT run_at, status FROM jobs WHERE kind = 'account.purge' AND account_id = ?",
                       (world.account_id,)).fetchone()
    assert (job[0], job[1]) == ((NOW + timedelta(days=7)).isoformat(), "QUEUED")
    kinds = [r[0] for r in conn.execute("SELECT kind FROM notifications WHERE account_id = ?", (world.account_id,))]
    assert "account.deletion_requested" in kinds
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE account_id = ?", (world.account_id,))]
    assert "ACCOUNT_DELETION_REQUESTED" in actions
    assert world.client.get("/api/notifications").status_code == 401  # the old session no longer works


def test_cancelling_during_cooling_off_restores_access_with_the_kill_switch_left_on(world):
    _request(world)
    cancel_deletion(world.conn, world.scope, now=NOW + timedelta(days=1), settings=world.settings)
    conn = world.conn
    assert conn.execute("SELECT status FROM accounts WHERE id = ?", (world.account_id,)).fetchone()[0] == "ACTIVE"
    assert conn.execute("SELECT status FROM users WHERE id = ?", (world.user_id,)).fetchone()[0] == "ACTIVE"
    assert kill_switch_state(conn, world.account_id)["engaged"]  # the user releases it
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'account.purge' AND account_id = ? AND status = "
                        "'QUEUED'", (world.account_id,)).fetchone()[0] == 0
    with pytest.raises(DeletionRefused):
        cancel_deletion(conn, world.scope, now=NOW, settings=world.settings)


def test_the_restricted_page_allows_export_and_keeping_the_account_only(world):
    form = {"password": PASSWORD, "email": "ada@example.com", "csrf_token": csrf_token(world.client)}
    response = world.client.post("/settings/account/delete", data=form, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/account/restricted"
    page = world.client.get("/account/restricted")
    assert 'data-restricted-page="DELETION_REQUESTED"' in page.text and "data-keep-account" in page.text
    assert world.client.get("/settings/account/export").status_code == 200
    assert world.client.get("/api/notifications").status_code == 403  # normal surfaces are closed
    home = world.client.get("/", follow_redirects=False)
    assert home.status_code == 303 and home.headers["location"] == "/account/restricted"
    kept = world.client.post("/settings/account/delete/cancel", data={"csrf_token": csrf_token(world.client)},
                             follow_redirects=False)
    assert kept.status_code == 303
    assert world.client.get("/api/notifications").status_code == 200


def _set_suspended(world, suspended: bool):
    """What the staff console's suspend/unsuspend write (account status + the audited action)."""
    from webapp.persistence.audit import audit
    conn = world.conn
    conn.execute("UPDATE accounts SET status = ? WHERE id = ?", ("SUSPENDED" if suspended else "ACTIVE",
                                                                 world.account_id))
    audit(conn, actor_type="STAFF", actor_id="staff_1", account_id=world.account_id,
          action="ACCOUNT_SUSPENDED" if suspended else "ACCOUNT_UNSUSPENDED", now=NOW, target_type="account",
          target_id=world.account_id)
    conn.commit()


def test_cancelling_a_deletion_requested_while_suspended_keeps_the_account_suspended(world):
    """A suspended user may request deletion (spec 22.2) but cancelling it must not lift the suspension."""
    _set_suspended(world, True)
    _request(world)
    cancel_deletion(world.conn, world.scope, now=NOW + timedelta(days=1), settings=world.settings)
    assert world.conn.execute("SELECT status FROM accounts WHERE id = ?", (world.account_id,)).fetchone()[0] \
        == "SUSPENDED"


def test_a_lifted_suspension_does_not_come_back_on_cancel(world):
    _set_suspended(world, True)
    _set_suspended(world, False)
    _request(world)
    cancel_deletion(world.conn, world.scope, now=NOW + timedelta(days=1), settings=world.settings)
    assert world.conn.execute("SELECT status FROM accounts WHERE id = ?", (world.account_id,)).fetchone()[0] \
        == "ACTIVE"
