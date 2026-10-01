"""Bundle 7 spec §7, §19 (Task 27): the staff console's sign-in, the §19.1
permission matrix, session kinds, re-authentication, the audit trail,
announcements, platform controls and the §8.6 live submission gate."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from product.autonomy_contract import Capability
from product.entitlements import FeatureNotInPlan
from product.submit_certification import FIXTURE_CERTIFIED, LIVE_CERTIFIED
from webapp.api.admin_api import ADMIN_ENDPOINTS
from webapp.app import create_app
from webapp.persistence.db import connect
from webapp.services import announcements
from webapp.services.entitlements import add_grant, set_platform_control
from webapp.services.staff_auth import ADMIN_PERMISSIONS, ROLES
from tests.webapp.admin_helpers import (
    STAFF_PASSWORD, admin_settings, code, customer, make_staff, staff_login, staff_post,
)
from tests.webapp.auth_helpers import csrf_token


@pytest.fixture
def world(tmp_path):
    settings = admin_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app):
        client = customer(app)
        account_id = client.get("/auth/me").json()["account_id"]
        yield SimpleNamespace(app=app, settings=settings, customer=client, account_id=account_id)


def _db(world):
    return connect(world.settings)


def _audit_actions(world) -> list[str]:
    conn = _db(world)
    try:
        return [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE actor_type = 'STAFF' ORDER BY seq")]
    finally:
        conn.close()


# ---- sign-in -----------------------------------------------------------------------------------

def test_totp_is_required_before_any_admin_page(world):
    staff = make_staff(world.settings, "ADMIN")
    client = TestClient(world.app)
    staff_post(client, "/admin/login", {"email": staff["email"], "password": STAFF_PASSWORD}, follow_redirects=False)
    page = client.get("/admin", follow_redirects=False)  # password only: no session yet
    assert page.status_code == 303 and page.headers["location"] == "/admin/login"
    assert client.get("/api/admin/dashboard").status_code == 401
    wrong = staff_post(client, "/admin/totp", {"code": "000000"}, follow_redirects=False)
    assert wrong.status_code == 401
    assert client.get("/api/admin/dashboard").status_code == 401


def test_the_first_sign_in_forces_totp_enrolment(world):
    staff = make_staff(world.settings, "SUPPORT", enrolled=False)
    client = TestClient(world.app)
    staff_post(client, "/admin/login", {"email": staff["email"], "password": STAFF_PASSWORD}, follow_redirects=False)
    page = client.get("/admin/totp")
    assert "data-totp-enrolment" in page.text and staff["secret"] in page.text
    staff_post(client, "/admin/totp", {"code": code(staff)}, follow_redirects=False)
    assert client.get("/api/admin/dashboard").status_code == 200
    again = TestClient(world.app)  # once confirmed, the secret is never shown again
    staff_post(again, "/admin/login", {"email": staff["email"], "password": STAFF_PASSWORD}, follow_redirects=False)
    assert staff["secret"] not in again.get("/admin/totp").text
    assert "STAFF_TOTP_ENROLLED" in _audit_actions(world)


def test_a_wrong_password_or_a_customer_is_refused_at_staff_sign_in(world):
    staff = make_staff(world.settings, "ADMIN")
    client = TestClient(world.app)
    assert staff_post(client, "/admin/login", {"email": staff["email"], "password": "nope-nope-nope"},
                      follow_redirects=False).status_code == 401
    from tests.webapp.auth_helpers import PASSWORD
    assert staff_post(client, "/admin/login", {"email": "ada@example.com", "password": PASSWORD},
                      follow_redirects=False).status_code == 401  # a customer is not staff


# ---- session kinds -----------------------------------------------------------------------------

def test_a_customer_session_is_refused_on_admin_routes(world):
    assert world.customer.get("/api/admin/dashboard").status_code == 403
    assert world.customer.get("/admin/accounts").status_code == 403


def test_a_staff_session_is_refused_on_user_routes(world):
    client = staff_login(world.app, make_staff(world.settings, "ADMIN"))
    assert client.get("/api/notifications").status_code == 403
    assert client.get("/settings/account").status_code == 403


# ---- the permission matrix ---------------------------------------------------------------------

def _fill(path: str, world) -> str:
    return (path.replace("{account_id}", world.account_id).replace("{grant_id}", "grant_missing")
            .replace("{job_id}", "job_missing").replace("{message_id}", "msg_missing")
            .replace("{announcement_id}", "ann_missing").replace("{user_id}", "user_missing"))


@pytest.mark.parametrize("role", ROLES)
def test_every_admin_endpoint_is_allowed_exactly_per_the_role_table(world, role):
    client = staff_login(world.app, make_staff(world.settings, role))
    token = csrf_token(client)
    assert ADMIN_ENDPOINTS, "the registry lists the endpoints"
    for method, path, permission in ADMIN_ENDPOINTS:
        url = _fill(path, world)
        response = client.get(url) if method == "GET" else client.post(url, headers={"X-CSRF-Token": token})
        allowed = permission in ADMIN_PERMISSIONS[role]
        refused = response.status_code == 403 and response.json().get("error") == "PERMISSION_DENIED"
        assert refused is not allowed, (role, method, path, response.status_code, response.text[:200])


def test_the_role_table_is_the_spec_table():
    view = {"accounts.view"}
    assert ADMIN_PERMISSIONS["SUPPORT"] == view | {"accounts.resend"}
    assert ADMIN_PERMISSIONS["OPERATIONS"] == view | {"accounts.resend", "accounts.suspend", "accounts.kill_switch",
                                                      "ops.retry", "announcements.manage"}
    assert ADMIN_PERMISSIONS["BILLING"] == view | {"billing.view", "grants.manage"}
    assert ADMIN_PERMISSIONS["ADMIN"] >= {"controls.manage", "staff.manage", "accounts.delete", "audit.all"}


# ---- re-authentication -------------------------------------------------------------------------

def _age_sessions(world, staff, minutes):
    conn = _db(world)
    try:
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        conn.execute("UPDATE web_sessions SET created_at = ? WHERE user_id = ?", (stamp, staff["id"]))
        conn.commit()
    finally:
        conn.close()


def test_destructive_actions_need_a_recent_authentication(world):
    staff = make_staff(world.settings, "OPERATIONS")
    client = staff_login(world.app, staff)
    _age_sessions(world, staff, 6)
    stale = staff_post(client, f"/api/admin/accounts/{world.account_id}/suspend", {"reason": "abuse"})
    assert stale.status_code == 403 and stale.json()["error"] == "REAUTH_REQUIRED"
    assert client.get(f"/api/admin/accounts/{world.account_id}").status_code == 200  # viewing is not destructive
    reauth = staff_post(client, "/admin/reauth", {"password": STAFF_PASSWORD, "code": code(staff),
                                                  "next": "/admin"}, follow_redirects=False)
    assert reauth.status_code == 303
    fresh = staff_post(client, f"/api/admin/accounts/{world.account_id}/suspend", {"reason": "abuse"})
    assert fresh.status_code == 200, fresh.text
    assert {"STAFF_REAUTHENTICATED", "ACCOUNT_SUSPENDED"} <= set(_audit_actions(world))


# ---- actions and the audit trail ---------------------------------------------------------------

def test_every_staff_action_writes_an_audit_row(world):
    admin = make_staff(world.settings, "ADMIN")
    client = staff_login(world.app, admin)
    other = make_staff(world.settings, "SUPPORT", "second@staff.example")
    base = f"/api/admin/accounts/{world.account_id}"
    steps = [
        (f"{base}/resend-verification", {}, "VERIFICATION_RESENT"),
        (f"{base}/password-reset", {}, "PASSWORD_RESET_SENT"),
        (f"{base}/kill-switch", {"engage": "true", "reason": "investigating"}, "AUTONOMY_KILL_SWITCH_SET"),
        (f"{base}/grants", {"kind": "ALLOWANCE_BONUS", "allowance": "applications.prepare", "amount": "5",
                            "reason": "goodwill"}, "ENTITLEMENT_GRANT_CREATED"),
        ("/api/admin/controls", {"key": "AI_ENABLED", "value": "true", "reason": "launch"}, "PLATFORM_CONTROL_SET"),
        (f"/api/admin/staff/{other['id']}/roles", {"role": "BILLING", "grant": "true", "reason": "cover"},
         "STAFF_ROLE_CHANGED"),
        (f"{base}/revoke-sessions", {}, "SESSIONS_REVOKED"),
        (f"{base}/suspend", {"reason": "abuse"}, "ACCOUNT_SUSPENDED"),
        (f"{base}/unsuspend", {"reason": "resolved"}, "ACCOUNT_UNSUSPENDED"),
    ]
    for url, data, action in steps:
        before = _audit_actions(world).count(action)
        response = staff_post(client, url, data)
        assert response.status_code == 200, (url, response.text)
        assert _audit_actions(world).count(action) == before + 1, action
    grant_id = staff_post(client, f"{base}/grants", {"kind": "ALLOWANCE_BONUS", "allowance": "applications.prepare",
                                                     "amount": "1", "reason": "x"}).json()["result"]
    assert staff_post(client, f"/api/admin/grants/{grant_id}/revoke").status_code == 200
    assert "ENTITLEMENT_GRANT_REVOKED" in _audit_actions(world)
    client.get(f"/admin/accounts/{world.account_id}")
    assert "ADMIN_ACCOUNT_VIEWED" in _audit_actions(world)


def test_suspension_revokes_access_and_the_user_sees_the_restricted_state(world):
    client = staff_login(world.app, make_staff(world.settings, "OPERATIONS"))
    assert staff_post(client, f"/api/admin/accounts/{world.account_id}/suspend", {"reason": "abuse"}).status_code == 200
    assert world.customer.get("/api/notifications").status_code == 401  # the customer's session was revoked
    conn = _db(world)
    try:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (world.account_id,)).fetchone()[0] == "SUSPENDED"
    finally:
        conn.close()
    assert staff_post(client, f"/api/admin/accounts/{world.account_id}/suspend",
                      {"reason": "again"}).json()["error"] == "INVALID_STATE"


def test_the_last_admin_cannot_be_removed(world):
    admin = make_staff(world.settings, "ADMIN")
    client = staff_login(world.app, admin)
    response = staff_post(client, f"/api/admin/staff/{admin['id']}/roles", {"role": "ADMIN", "grant": "false",
                                                                           "reason": "oops"})
    assert response.status_code == 409


# ---- announcements -----------------------------------------------------------------------------

def test_announcement_markdown_is_sanitized():
    html = announcements.render_markdown(
        "Hello <script>alert(1)</script> **world**\n\n- [safe](https://example.com)\n- [bad](javascript:alert(1))\n"
        "- <img src=x onerror=alert(1)>")
    assert "<script" not in html and "javascript:" not in html and "<img" not in html
    assert "&lt;script&gt;" in html and "<strong>world</strong>" in html
    assert '<a href="https://example.com" rel="noopener noreferrer">safe</a>' in html
    assert "<li>bad</li>" in html  # the unsafe link keeps its text only


def test_announcements_reach_their_audience_only(world):
    power_client = TestClient(world.app)
    from tests.webapp.auth_helpers import sign_in, sign_up_and_verify
    sign_up_and_verify(power_client, email="grace@example.com", name="Grace Hopper")
    sign_in(power_client, email="grace@example.com")
    power_account = power_client.get("/auth/me").json()["account_id"]
    now = datetime.now(timezone.utc)
    conn = _db(world)
    try:
        add_grant(conn, account_id=power_account, kind="PLAN_OVERRIDE", plan_id="power", reason="t", actor_user_id=None,
                  starts_at=now - timedelta(days=1), expires_at=now + timedelta(days=30), now=now)
        conn.commit()
    finally:
        conn.close()
    client = staff_login(world.app, make_staff(world.settings, "OPERATIONS"))
    created = staff_post(client, "/api/admin/announcements", {"title": "Power news", "body_markdown": "For **Power**.",
                                                              "audience": "PLAN:power", "severity": "INFO"})
    announcement_id = created.json()["result"]
    published = staff_post(client, f"/api/admin/announcements/{announcement_id}/publish")
    assert published.json()["result"] == 1
    conn = _db(world)
    try:
        kinds = {r[0]: r[1] for r in conn.execute("SELECT account_id, COUNT(*) FROM notifications WHERE kind = "
                                                  "'announcement.published' GROUP BY account_id")}
        assert kinds == {power_account: 1}
        assert [a["title"] for a in announcements.active_for_account(conn, power_account, settings=world.settings,
                                                                     now=datetime.now(timezone.utc))] == ["Power news"]
        assert announcements.active_for_account(conn, world.account_id, settings=world.settings,
                                                now=datetime.now(timezone.utc)) == []
    finally:
        conn.close()
    staff_post(client, f"/api/admin/announcements/{announcement_id}/withdraw")
    conn = _db(world)
    try:
        assert announcements.active_for_account(conn, power_account, settings=world.settings,
                                                now=datetime.now(timezone.utc)) == []
    finally:
        conn.close()


# ---- platform controls -------------------------------------------------------------------------

def test_turning_ai_off_refuses_prepare_platform_wide(world):
    client = staff_login(world.app, make_staff(world.settings, "ADMIN"))
    assert staff_post(client, "/api/admin/controls", {"key": "AI_ENABLED", "value": "false",
                                                      "reason": "provider incident"}).status_code == 200
    gate = world.app.state.entitlement_gate
    conn = _db(world)
    try:
        with pytest.raises(FeatureNotInPlan):
            gate.require_feature(conn, SimpleNamespace(account_id=world.account_id), "ai.prepare",
                                 now=datetime.now(timezone.utc))
    finally:
        conn.close()
    response = world.customer.post("/api/workspaces/ws_missing/understand", json={"request_id": "r"},
                                   headers={"X-CSRF-Token": csrf_token(world.customer)})
    assert response.status_code in (402, 404)


def test_a_control_change_needs_a_reason(world):
    client = staff_login(world.app, make_staff(world.settings, "ADMIN"))
    response = staff_post(client, "/api/admin/controls", {"key": "AI_ENABLED", "value": "false", "reason": " "})
    assert response.status_code == 400


# ---- §8.6 the live submission gate -------------------------------------------------------------

LIVE = SimpleNamespace(status=LIVE_CERTIFIED, live_evidence={"run": "evidence"})


def _hosted(world, **overrides):
    return dataclasses.replace(world.settings, **{"deployment": "hosted", "human_submit_enabled": True, **overrides})


def _controls(world, *, submit: bool, signed_off: bool):
    conn = _db(world)
    now = datetime.now(timezone.utc)
    set_platform_control(conn, "SUBMIT_ENABLED", submit, actor_user_id=None, reason="t", now=now)
    set_platform_control(conn, "HOSTED_THREAT_MODEL_SIGNED_OFF", signed_off, actor_user_id=None, reason="t", now=now)
    conn.commit()
    return conn


def test_hosted_submit_needs_every_gate(world):
    conn = _controls(world, submit=True, signed_off=True)
    try:
        assert _hosted(world).human_submit_ceiling(conn, certification=LIVE) is Capability.SUBMIT
        assert _hosted(world, human_submit_enabled=False).human_submit_ceiling(conn, certification=LIVE) is Capability.FILL
        assert _hosted(world).human_submit_ceiling(conn, certification=SimpleNamespace(
            status=LIVE_CERTIFIED, live_evidence=None)) is Capability.FILL
        assert _hosted(world).human_submit_ceiling(conn, certification=None) is Capability.FILL
        assert _hosted(world).human_submit_ceiling(None, certification=LIVE) is Capability.FILL
    finally:
        conn.close()
    for submit, signed_off in ((False, True), (True, False)):
        conn = _controls(world, submit=submit, signed_off=signed_off)
        try:
            assert _hosted(world).human_submit_ceiling(conn, certification=LIVE) is Capability.FILL
        finally:
            conn.close()


def test_greenhouse_is_fill_in_hosted_mode_even_with_every_control_on(world):
    from product.submit_certification import SUBMIT_CATALOGUE
    greenhouse = next(cert for (adapter, _), cert in SUBMIT_CATALOGUE.items() if "greenhouse" in adapter)
    assert greenhouse.status == FIXTURE_CERTIFIED
    conn = _controls(world, submit=True, signed_off=True)
    try:
        assert _hosted(world).human_submit_ceiling(conn, certification=greenhouse) is Capability.FILL
    finally:
        conn.close()


def test_local_mode_submit_ceiling_is_unchanged(world):
    local = dataclasses.replace(world.settings, human_submit_enabled=True)
    assert local.human_submit_ceiling() is Capability.SUBMIT
    assert local.human_submit_ceiling(None, certification=None) is Capability.SUBMIT
    assert dataclasses.replace(world.settings, human_submit_enabled=False).human_submit_ceiling() is Capability.FILL
