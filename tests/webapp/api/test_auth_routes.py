"""Bundle 7 Task 8: sign-up, verification, login, reset, email change, logout (spec §7)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect
from webapp.services import auth as auth_service
from tests.webapp.auth_helpers import mail, post

PASSWORD = "correct horse battery staple"


@pytest.fixture
def world(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[2] / "fixtures" / "extensions",
                        auth_required_in_local=True)
    _CURRENT["settings"] = settings
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        conn.execute("INSERT INTO legal_documents (id, kind, version, published_at, content_sha256) "
                     "VALUES ('terms_v1', 'TERMS', '1', '2026-09-01T00:00:00+00:00', ?)", ("a" * 64,))
        conn.execute("INSERT INTO legal_documents (id, kind, version, published_at, content_sha256) "
                     "VALUES ('privacy_v1', 'PRIVACY', '1', '2026-09-01T00:00:00+00:00', ?)", ("b" * 64,))
        conn.commit()
        conn.close()
        yield client, settings


def _signup(client, email="ada@example.com", password=PASSWORD, name="Ada Lovelace"):
    return post(client, "/auth/signup", data={"email": email, "password": password, "display_name": name,
                                             "accept_terms": "terms_v1", "accept_privacy": "privacy_v1"})


_CURRENT: dict = {}  # the running test's settings (set by the world fixture)


def _mail(template_id, to=None):
    """Real outbound_messages rows (Bundle 7 Task 19 replaced the Task 8 stub)."""
    return mail(_CURRENT["settings"], template_id, to)


def _token(template_id, to=None):
    return _mail(template_id, to)[-1]["payload"]["token"]


def _login(client, email="ada@example.com", password=PASSWORD):
    return post(client, "/auth/login", data={"email": email, "password": password}, follow_redirects=False)


def _verified(client, email="ada@example.com"):
    _signup(client, email=email)
    post(client, "/auth/verify-email", data={"token": _token("auth.verify_email", email)})


def test_signup_is_uniform_for_new_and_existing_emails(world):
    client, _ = world
    first = _signup(client)
    assert first.status_code == 200
    assert len(_mail("auth.verify_email", "ada@example.com")) == 1
    second = _signup(client, email=" ADA@Example.com ")
    assert second.status_code == 200 and second.text == first.text
    assert len(_mail("auth.account_exists", "ada@example.com")) == 1
    assert len(_mail("auth.verify_email")) == 1


def test_signup_validation_errors_are_shown(world):
    client, _ = world
    weak = _signup(client, password="short")
    assert weak.status_code == 400 and "at least 12 characters" in weak.text
    missing_terms = post(client, "/auth/signup", data={"email": "b@example.com", "password": PASSWORD,
                                                      "display_name": "B", "accept_privacy": "privacy_v1"})
    assert missing_terms.status_code == 400 and "Terms" in missing_terms.text


def test_signup_without_published_legal_documents_is_unavailable(world):
    client, settings = world
    conn = connect(settings.db_path)
    conn.execute("DELETE FROM legal_documents")
    conn.commit()
    response = _signup(client)
    assert response.status_code == 503 and "SIGNUP_UNAVAILABLE" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    conn.close()


def test_login_before_verification_gives_an_unverified_session_then_verify_activates(world):
    client, _ = world
    _signup(client)
    response = _login(client)
    assert response.status_code == 303 and response.headers["location"] == "/check-email"
    assert client.get("/auth/me").json()["email_verified"] is False
    verify = post(client, "/auth/verify-email", data={"token": _token("auth.verify_email")}, follow_redirects=False)
    assert verify.status_code == 303
    me = client.get("/auth/me").json()
    assert (me["email_verified"], me["status"]) == (True, "ACTIVE")


def test_wrong_password_and_unknown_email_look_identical_and_both_verify_a_hash(world, monkeypatch):
    client, _ = world
    _verified(client)
    post(client, "/auth/logout")
    calls = []
    real = auth_service.verify_password
    monkeypatch.setattr(auth_service, "verify_password", lambda h, p: calls.append(h) or real(h, p))
    wrong = _login(client, password="wrong password here")
    unknown = _login(client, email="nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.text == unknown.text
    assert len(calls) == 2  # one argon2 verification per attempt, known or not
    assert "__Host-js_session" not in wrong.headers.get("set-cookie", "") and "js_session=" not in wrong.headers.get("set-cookie", "")


def test_login_accepts_an_equivalent_email(world):
    client, _ = world
    _verified(client, email="foo@example.com")
    post(client, "/auth/logout")
    assert _login(client, email=" FOO@example.com").status_code == 303
    assert client.get("/auth/me").json()["email"] == "foo@example.com"


def test_session_cookie_attributes_in_local_auth_mode(world):
    client, _ = world
    _verified(client)
    response = _login(client)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("js_session=") and "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert "Path=/" in cookie


def test_logout_clears_the_cookie_and_revokes_the_session(world):
    client, _ = world
    _verified(client)
    _login(client)
    assert client.get("/auth/me").status_code == 200
    response = post(client, "/auth/logout", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/auth/me").status_code == 401


def test_password_reset_revokes_other_sessions_and_signs_in_fresh(world):
    client, settings = world
    _verified(client)
    _login(client)
    other = TestClient(client.app)
    _login(other)
    assert other.get("/auth/me").status_code == 200
    reset_request = post(client, "/auth/password-reset/request", data={"email": "ada@example.com"})
    unknown_request = post(client, "/auth/password-reset/request", data={"email": "ghost@example.com"})
    assert reset_request.text == unknown_request.text
    new_password = "another long passphrase 42"
    confirm = post(client, "/auth/password-reset/confirm",
                          data={"token": _token("auth.password_reset"), "password": new_password},
                          follow_redirects=False)
    assert confirm.status_code == 303
    assert other.get("/auth/me").status_code == 401  # other session revoked
    assert client.get("/auth/me").status_code == 200  # signed in fresh
    post(client, "/auth/logout")
    assert _login(client, password=PASSWORD).status_code == 401
    assert _login(client, password=new_password).status_code == 303
    assert len(_mail("auth.password_changed", "ada@example.com")) == 1


def test_change_password_needs_the_current_one_and_revokes_other_sessions(world):
    client, _ = world
    _verified(client)
    _login(client)
    other = TestClient(client.app)
    _login(other)
    refused = post(client, "/settings/password", data={"current_password": "nope nope nope",
                                                      "new_password": "brand new passphrase 1"})
    assert refused.status_code == 400
    ok = post(client, "/settings/password", data={"current_password": PASSWORD,
                                                 "new_password": "brand new passphrase 1"})
    assert ok.status_code == 200
    assert client.get("/auth/me").status_code == 200 and other.get("/auth/me").status_code == 401


def test_email_change_notifies_the_old_address_and_applies_on_confirm(world):
    client, _ = world
    _verified(client)
    _login(client)
    response = post(client, "/settings/email", data={"new_email": "ada.new@example.com", "password": PASSWORD})
    assert response.status_code == 200
    assert len(_mail("auth.email_change_notice", "ada@example.com")) == 1
    assert client.get("/auth/me").json()["email"] == "ada@example.com"
    token = _token("auth.email_change_confirm", "ada.new@example.com")
    assert post(client, "/auth/email-change/confirm", data={"token": token}, follow_redirects=False).status_code == 303
    assert client.get("/auth/me").json()["email"] == "ada.new@example.com"


def test_resend_verification_is_uniform(world):
    client, _ = world
    _signup(client)
    known = post(client, "/auth/resend-verification", data={"email": "ada@example.com"})
    unknown = post(client, "/auth/resend-verification", data={"email": "ghost@example.com"})
    assert known.status_code == unknown.status_code == 200 and known.text == unknown.text
    assert len(_mail("auth.verify_email", "ada@example.com")) == 2


def test_auth_pages_render(world):
    client, _ = world
    for page in ("/signup", "/login", "/reset-password", "/check-email"):
        assert client.get(page).status_code == 200, page
    assert "Terms" in client.get("/signup").text


def test_signed_out_home_is_the_public_landing(world):
    client, _ = world
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/signup"' in response.text and "Application status" not in response.text
    _verified(client)
    _login(client)
    assert "Sign out Ada Lovelace" in client.get("/").text


def _notification_kinds(settings):
    conn = connect(settings.db_path)
    try:
        return [r[0] for r in conn.execute("SELECT kind FROM notifications ORDER BY created_at, id")]
    finally:
        conn.close()


def test_password_and_email_changes_are_security_notifications_without_a_second_email(world):
    client, settings = world
    _verified(client)
    _login(client)
    post(client, "/settings/password", data={"current_password": PASSWORD, "new_password": "brand new passphrase 1"})
    assert _notification_kinds(settings) == ["security.password_changed"]
    assert len(_mail("auth.password_changed")) == 1 and _mail("notify.immediate") == []
    post(client, "/settings/email", data={"new_email": "ada.new@example.com", "password": "brand new passphrase 1"})
    token = _token("auth.email_change_confirm", "ada.new@example.com")
    post(client, "/auth/email-change/confirm", data={"token": token}, follow_redirects=False)
    assert _notification_kinds(settings) == ["security.password_changed", "security.email_changed"]
    assert _mail("notify.immediate") == []


def test_signup_pays_for_one_password_hash_whether_or_not_the_email_exists(world, monkeypatch):
    """Spec 7: the existing-email response is in the same timing class as a new signup."""
    client, _ = world
    calls = []
    real = auth_service.hash_password
    monkeypatch.setattr(auth_service, "hash_password", lambda p: calls.append(p) or real(p))
    _signup(client)
    _signup(client, email=" ADA@Example.com ")
    assert len(calls) == 2  # one argon2 hash per attempt, new or existing
