"""Staff TOTP secrets at rest (Bundle 7 Task 27, T16): AES-256-GCM under a
key ring from hosted configuration, with key ids for rotation; every failure
is a deterministic error, and the plaintext secret leaks nowhere."""
from __future__ import annotations

import base64
import dataclasses
import logging
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.persistence.db import connect
from webapp.services import secret_box, staff_auth
from webapp.services.secret_box import SecretBoxError
from tests.webapp.admin_helpers import admin_settings, code, make_staff, staff_login, staff_post


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(admin_settings(tmp_path), totp_encryption_keys=f"k1:{_key()}")


def test_round_trip_carries_version_and_key_id_not_the_plaintext(settings):
    record = secret_box.encrypt(settings, "JBSWY3DPEHPK3PXP", purpose="staff_totp:user_1")
    assert record.startswith("v2.k1.") and "JBSWY3DPEHPK3PXP" not in record
    assert secret_box.decrypt(settings, record, purpose="staff_totp:user_1") == "JBSWY3DPEHPK3PXP"
    assert secret_box.encrypt(settings, "JBSWY3DPEHPK3PXP", purpose="staff_totp:user_1") != record  # fresh nonce


@pytest.mark.parametrize("position", [8, -1, -20])
def test_any_tampering_is_rejected(settings, position):
    record = secret_box.encrypt(settings, "SECRET", purpose="p")
    head, body = record.rsplit(".", 1)
    raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    raw[position] ^= 0x01
    tampered = f"{head}.{base64.urlsafe_b64encode(bytes(raw)).decode().rstrip('=')}"
    with pytest.raises(SecretBoxError):
        secret_box.decrypt(settings, tampered, purpose="p")


def test_a_record_is_bound_to_its_purpose(settings):
    record = secret_box.encrypt(settings, "SECRET", purpose="staff_totp:user_1")
    with pytest.raises(SecretBoxError):
        secret_box.decrypt(settings, record, purpose="staff_totp:user_2")  # moved to another user's row


def test_a_wrong_key_an_unknown_key_id_and_bad_formats_fail(settings):
    record = secret_box.encrypt(settings, "SECRET", purpose="p")
    with pytest.raises(SecretBoxError):
        secret_box.decrypt(dataclasses.replace(settings, totp_encryption_keys=f"k1:{_key()}"), record, purpose="p")
    with pytest.raises(SecretBoxError, match="no key k1"):
        secret_box.decrypt(dataclasses.replace(settings, totp_encryption_keys=f"k9:{_key()}"), record, purpose="p")
    for bad in ("v1.k1.AAAA", "garbage", "v2.k1.", "v2.k1.AA"):
        with pytest.raises(SecretBoxError):
            secret_box.decrypt(settings, bad, purpose="p")


def test_a_missing_key_ring_fails_in_hosted_mode(settings):
    hosted = dataclasses.replace(settings, deployment="hosted", totp_encryption_keys=None)
    with pytest.raises(SecretBoxError, match="required in hosted mode"):
        secret_box.encrypt(hosted, "SECRET", purpose="p")


@pytest.mark.parametrize("spec", ["", "k1", "k1:short", f"k1:{_key()},k1:{_key()}", f"a.b:{_key()}"])
def test_malformed_key_rings_are_refused(spec):
    with pytest.raises(SecretBoxError):
        secret_box.parse_key_ring(spec)


def test_rotation_keeps_old_records_readable_and_reencrypts_them(settings):
    from webapp.persistence.db import init_db
    init_db(settings)
    conn = connect(settings)
    try:
        staff = make_staff(settings, "ADMIN")
        old_record = conn.execute("SELECT secret_encrypted FROM staff_totp WHERE user_id = ?",
                                  (staff["id"],)).fetchone()[0]
        old_ring = settings.totp_encryption_keys
        rotated = dataclasses.replace(settings, totp_encryption_keys=f"k2:{_key()},{old_ring}")
        assert secret_box.needs_rotation(rotated, old_record)
        assert staff_auth.verify_totp(conn, rotated, staff["id"], code(staff), now=datetime.now(timezone.utc))
        assert staff_auth.rotate_totp_secrets(conn, rotated) == 1
        conn.commit()
        new_record = conn.execute("SELECT secret_encrypted FROM staff_totp WHERE user_id = ?",
                                  (staff["id"],)).fetchone()[0]
        assert new_record.startswith("v2.k2.") and not secret_box.needs_rotation(rotated, new_record)
        only_new = dataclasses.replace(settings, totp_encryption_keys=rotated.totp_encryption_keys.split(",")[0])
        assert staff_auth.verify_totp(conn, only_new, staff["id"], code(staff), now=datetime.now(timezone.utc))
        assert staff_auth.rotate_totp_secrets(conn, rotated) == 0
    finally:
        conn.close()


def test_the_plaintext_secret_appears_only_on_the_enrolment_page(settings, caplog):
    caplog.set_level(logging.DEBUG)
    app = create_app(settings)
    with TestClient(app):
        staff = make_staff(settings, "ADMIN", enrolled=False)
        client = TestClient(app)
        staff_post(client, "/admin/login", {"email": staff["email"], "password": "staff-password-123"},
                   follow_redirects=False)
        enrolment = client.get("/admin/totp")
        assert staff["secret"] in enrolment.text and "no-store" in enrolment.headers.get("cache-control", "")
        staff_post(client, "/admin/totp", {"code": "000000"})  # a failure is audited
        staff_post(client, "/admin/totp", {"code": code(staff)}, follow_redirects=False)
        bodies = [client.get(url).text for url in ("/admin/staff", "/api/admin/staff", "/admin/audit",
                                                   "/api/admin/audit", "/admin")]
        conn = connect(settings)
        try:
            stored = [str(v) for row in conn.execute("SELECT * FROM audit_log").fetchall() for v in tuple(row)]
            stored += [str(v) for row in conn.execute("SELECT * FROM outbound_messages").fetchall() for v in tuple(row)]
            stored += [str(v) for row in conn.execute("SELECT * FROM staff_totp").fetchall() for v in tuple(row)]
        finally:
            conn.close()
    for text in bodies + stored + [caplog.text]:
        assert staff["secret"] not in text
