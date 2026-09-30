"""Bundle 7 spec X2-X4, §9: device credentials, pairing codes and handoff tickets."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from webapp import comms
from webapp.persistence import identity
from webapp.persistence.db import connect, init_db
from webapp.services import extension_auth as ext
from webapp.services import notifications
from webapp.storage.profile_sources import DatabaseProfileSourceStore

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
SECRET = "k" * 43


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    conn = connect(path)
    users = []
    for email in ("ada@example.com", "bob@example.com"):
        created = identity.create_user_with_account(
            conn, email=email, password_hash="h", display_name=email.split("@")[0], legal_document_ids=[],
            now=NOW, profile_store=DatabaseProfileSourceStore())
        users.append((created["user"]["id"], created["account"]["id"]))
    conn.commit()
    comms.reset_outbox_for_tests()
    notifications.reset_for_tests()
    yield conn, users
    conn.close()


def _pair(conn, user, now=NOW):
    user_id, account_id = user
    _, code = ext.create_pairing_code(conn, account_id=account_id, user_id=user_id, now=now)
    return ext.pair_device(conn, raw_code=code, device_label="Chrome on Windows", now=now, secret=SECRET)


def test_pairing_code_is_human_typeable_single_use_and_expires(world):
    conn, users = world
    user_id, account_id = users[0]
    _, code = ext.create_pairing_code(conn, account_id=account_id, user_id=user_id, now=NOW)
    assert len(code) == 10 and set(code) <= set(ext.CROCKFORD)
    result = ext.pair_device(conn, raw_code=code.lower(), device_label="x", now=NOW, secret=SECRET)
    principal = ext.resolve_access(conn, result.access_token, now=NOW)
    assert (principal.account_id, principal.user_id) == (account_id, user_id)
    with pytest.raises(ext.PairingCodeInvalid):
        ext.pair_device(conn, raw_code=code, device_label="x", now=NOW, secret=SECRET)
    _, late = ext.create_pairing_code(conn, account_id=account_id, user_id=user_id, now=NOW)
    with pytest.raises(ext.PairingCodeInvalid):
        ext.pair_device(conn, raw_code=late, device_label="x", now=NOW + timedelta(minutes=10, seconds=1),
                        secret=SECRET)


def test_pairing_audits_and_sends_the_new_device_email(world):
    conn, users = world
    result = _pair(conn, users[0])
    conn.commit()
    assert result.account_label == "a***@example.com"
    assert [m["template_id"] for m in comms.outbox_for_tests()] == ["security.new_device_paired"]
    assert conn.execute("SELECT action FROM audit_log").fetchone()["action"] == "EXTENSION_DEVICE_PAIRED"


def test_access_tokens_expire_after_ten_minutes(world):
    conn, users = world
    result = _pair(conn, users[0])
    assert ext.resolve_access(conn, result.access_token, now=NOW + timedelta(minutes=9, seconds=59)) is not None
    assert ext.resolve_access(conn, result.access_token, now=NOW + timedelta(minutes=10, seconds=1)) is None
    assert ext.resolve_access(conn, "garbage", now=NOW) is None


def test_refresh_rotates_and_reuse_revokes_the_whole_device(world):
    conn, users = world
    first = _pair(conn, users[0])
    second = ext.refresh(conn, device_id=first.device_id, raw_refresh=first.refresh_token,
                         now=NOW + timedelta(minutes=11))
    assert second.refresh_token != first.refresh_token
    assert ext.resolve_access(conn, second.access_token, now=NOW + timedelta(minutes=12)) is not None
    with pytest.raises(ext.DeviceRevoked):
        ext.refresh(conn, device_id=first.device_id, raw_refresh=first.refresh_token, now=NOW + timedelta(minutes=13))
    conn.commit()
    # the reuse killed the legitimate new tokens too
    assert ext.resolve_access(conn, second.access_token, now=NOW + timedelta(minutes=13)) is None
    with pytest.raises(ext.DeviceRevoked):
        ext.refresh(conn, device_id=first.device_id, raw_refresh=second.refresh_token, now=NOW + timedelta(minutes=14))
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY seq")]
    assert "EXTENSION_TOKEN_REUSE" in actions
    assert [n["kind"] for n in notifications.recorded_for_tests()] == ["security.token_reuse_detected"]


def test_refresh_tokens_last_thirty_days_sliding(world):
    conn, users = world
    first = _pair(conn, users[0])
    later = NOW + timedelta(days=29)
    second = ext.refresh(conn, device_id=first.device_id, raw_refresh=first.refresh_token, now=later)
    third = ext.refresh(conn, device_id=first.device_id, raw_refresh=second.refresh_token, now=later + timedelta(days=29))
    assert third.access_token
    with pytest.raises(ext.DeviceRevoked):
        ext.refresh(conn, device_id=first.device_id, raw_refresh=third.refresh_token,
                    now=later + timedelta(days=29 + 31))


def test_refresh_needs_the_matching_device(world):
    conn, users = world
    ada, bob = _pair(conn, users[0]), _pair(conn, users[1])
    with pytest.raises(ext.DeviceRevoked):
        ext.refresh(conn, device_id=bob.device_id, raw_refresh=ada.refresh_token, now=NOW)


def test_revoking_all_devices_for_a_user(world):
    conn, users = world
    one, two = _pair(conn, users[0]), _pair(conn, users[0])
    other = _pair(conn, users[1])
    assert ext.revoke_all_devices_for_user(conn, users[0][0], reason="PASSWORD_CHANGED", now=NOW) == 2
    assert ext.resolve_access(conn, one.access_token, now=NOW) is None
    assert ext.resolve_access(conn, two.access_token, now=NOW) is None
    assert ext.resolve_access(conn, other.access_token, now=NOW) is not None


def test_handoff_tickets_are_bound_single_use_and_short_lived(world):
    conn, users = world
    ada, bob = _pair(conn, users[0]), _pair(conn, users[1])
    ada_principal = ext.resolve_access(conn, ada.access_token, now=NOW)
    bob_principal = ext.resolve_access(conn, bob.access_token, now=NOW)
    user_id, account_id = users[0]

    def issue(now=NOW):
        return ext.issue_handoff_ticket(conn, account_id=account_id, user_id=user_id, workspace_id="ws_1",
                                        purpose="HANDOFF", now=now, secret=SECRET)

    ticket = issue()
    with pytest.raises(ext.AccountMismatch):
        ext.consume_handoff_ticket(conn, ticket, principal=bob_principal, workspace_id="ws_1", purpose="HANDOFF",
                                   now=NOW, secret=SECRET)
    ext.consume_handoff_ticket(conn, ticket, principal=ada_principal, workspace_id="ws_1", purpose="HANDOFF",
                               now=NOW, secret=SECRET)
    with pytest.raises(ext.TicketInvalid):  # replay
        ext.consume_handoff_ticket(conn, ticket, principal=ada_principal, workspace_id="ws_1", purpose="HANDOFF",
                                   now=NOW, secret=SECRET)
    tampered = issue()
    head, sig = tampered.rsplit(".", 1)
    with pytest.raises(ext.TicketInvalid):
        ext.consume_handoff_ticket(conn, head + "." + ("0" * len(sig)), principal=ada_principal, workspace_id="ws_1",
                                   purpose="HANDOFF", now=NOW, secret=SECRET)
    stale = issue()
    with pytest.raises(ext.TicketInvalid):
        ext.consume_handoff_ticket(conn, stale, principal=ada_principal, workspace_id="ws_1", purpose="HANDOFF",
                                   now=NOW + timedelta(minutes=5, seconds=1), secret=SECRET)
    wrong_workspace = issue()
    with pytest.raises(ext.TicketInvalid):
        ext.consume_handoff_ticket(conn, wrong_workspace, principal=ada_principal, workspace_id="ws_2",
                                   purpose="HANDOFF", now=NOW, secret=SECRET)


def test_local_mode_pairs_without_a_user(world):
    conn, _ = world
    _, code = ext.create_pairing_code(conn, account_id="account_local", user_id=None, now=NOW)
    result = ext.pair_device(conn, raw_code=code, device_label="local", now=NOW, secret=SECRET)
    principal = ext.resolve_access(conn, result.access_token, now=NOW)
    assert (principal.account_id, principal.user_id) == ("account_local", None)
    assert result.account_label == "Local user"
