"""Bundle 7 Task 19: the generic normalized email-provider webhook. The
adapter verifies and normalizes; the route stores each event once and applies
bounces and complaints as suppressions."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.comms.outbox import address_hash
from webapp.config import Settings
from webapp.persistence.db import connect


@dataclass
class Event:
    provider_event_id: str
    kind: str
    address: str | None


class WebhookProvider:
    name = "fakemail"

    def send(self, **kwargs):
        return "id"

    def verify_webhook(self, *, headers, body, now):
        if headers.get("x-signature") != "ok":
            raise ValueError("bad signature")
        return [Event("evt-1", "COMPLAINT", "ada@example.com"), Event("evt-2", "DELIVERED", "bob@example.com")]


def _client(tmp_path, provider):
    app = create_app(Settings(db_path=tmp_path / "db.sqlite3"))
    app.state.email_provider = provider
    return app


def test_a_verified_event_is_stored_once_and_suppresses(tmp_path):
    app = _client(tmp_path, WebhookProvider())
    with TestClient(app) as client:
        for _ in range(2):  # redelivered
            response = client.post("/webhooks/email/fakemail", content=b"{}", headers={"x-signature": "ok"})
            assert response.status_code == 200, response.text
    conn = connect(tmp_path / "db.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM email_provider_events").fetchone()[0] == 2
        assert [tuple(r) for r in conn.execute("SELECT address_hash, reason FROM email_suppressions")] == [
            (address_hash("ada@example.com"), "COMPLAINT")]
    finally:
        conn.close()


def test_an_unverified_event_is_refused(tmp_path):
    app = _client(tmp_path, WebhookProvider())
    with TestClient(app) as client:
        response = client.post("/webhooks/email/fakemail", content=b"{}", headers={"x-signature": "forged"})
    assert response.status_code == 400 and response.json()["error"] == "WEBHOOK_SIGNATURE_INVALID"


def test_a_provider_without_webhooks_or_another_name_is_404(tmp_path):
    with TestClient(create_app(Settings(db_path=tmp_path / "db.sqlite3"))) as client:  # console provider
        assert client.post("/webhooks/email/console", content=b"{}").status_code == 404
    app = _client(tmp_path, WebhookProvider())
    with TestClient(app) as client:
        assert client.post("/webhooks/email/other", content=b"{}").status_code == 404
