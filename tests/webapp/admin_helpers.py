"""Staff fixtures for the admin console tests (Bundle 7 Task 27)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pyotp
from fastapi.testclient import TestClient

from webapp.config import Settings
from webapp.persistence.db import connect
from webapp.services import staff_auth
from webapp.services.passwords import hash_password
from tests.webapp.auth_helpers import PASSWORD, csrf_token, publish_legal_documents

STAFF_PASSWORD = "staff-password-123"
SECRET_KEY = "test-secret-key-for-staff-console"


def admin_settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                    extensions_dir=Path(__file__).parent / "fixtures" / "extensions", auth_required_in_local=True,
                    secret_key=SECRET_KEY)


def make_staff(settings: Settings, role: str, email: str | None = None, *, enrolled: bool = True) -> dict:
    """A staff user with one role; ``enrolled`` confirms the TOTP enrolment."""
    now = datetime.now(timezone.utc)
    email = email or f"{role.lower()}@staff.example"
    conn = connect(settings)
    try:
        user_id = staff_auth.create_staff_user(conn, email=email, password_hash=hash_password(STAFF_PASSWORD),
                                               display_name=role.title(), now=now)
        staff_auth.grant_role(conn, user_id=user_id, role=role, actor_user_id=None, reason="test", now=now)
        secret = staff_auth.start_totp_enrolment(conn, settings, user_id)
        if enrolled:
            conn.execute("UPDATE staff_totp SET confirmed_at = ? WHERE user_id = ?", (now.isoformat(), user_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": user_id, "email": email, "secret": secret, "role": role}


def code(staff: dict) -> str:
    return pyotp.TOTP(staff["secret"]).now()


def staff_post(client: TestClient, url: str, data: dict | None = None, **kwargs):
    return client.post(url, data=data or {}, headers={"X-CSRF-Token": csrf_token(client)}, **kwargs)


def staff_login(app, staff: dict) -> TestClient:
    """A new client signed in to the console (password, then TOTP)."""
    client = TestClient(app)
    first = staff_post(client, "/admin/login", {"email": staff["email"], "password": STAFF_PASSWORD},
                       follow_redirects=False)
    assert first.status_code == 303 and first.headers["location"] == "/admin/totp", first.text
    second = staff_post(client, "/admin/totp", {"code": code(staff)}, follow_redirects=False)
    assert second.status_code == 303 and second.headers["location"] == "/admin", second.text
    return client


def customer(app) -> TestClient:
    from tests.webapp.auth_helpers import sign_in, sign_up_and_verify
    client = TestClient(app)
    publish_legal_documents(app.state.settings)
    sign_up_and_verify(client)
    sign_in(client)
    return client


__all__ = ["PASSWORD", "STAFF_PASSWORD", "admin_settings", "code", "customer", "make_staff", "staff_login",
           "staff_post"]
