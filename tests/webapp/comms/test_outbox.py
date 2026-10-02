"""Bundle 7 Task 19 (spec §18.1-§18.2): the transactional outbox and its
dispatcher: suppression, the marketing refusal, backoff and idempotency."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from webapp.comms.console import ConsoleEmailProvider
from webapp.comms.email_port import PermanentSendError, TransientSendError
from webapp.comms.outbox import (
    TRANSIENT_BACKOFF_SECONDS, address_hash, claim_due, dispatch_one, enqueue, record_email_event, suppress,
)
from webapp.config import Settings
from webapp.persistence.db import connect, init_db

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
ORIGIN = "https://app.example.test"


class FlakyProvider:
    name = "flaky"

    def __init__(self, failures):
        self.failures = list(failures)
        self.sent = []

    def send(self, *, to, subject, text, html, idempotency_key, headers):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append({"to": to, "subject": subject, "idempotency_key": idempotency_key, "headers": headers})
        return f"msg-{len(self.sent)}"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    connection = connect(path)
    yield connection
    connection.close()


def _enqueue(conn, template_id="auth.password_changed", *, category="SERVICE", to="ada@example.com", key="k1",
             payload=None, now=NOW):
    message_id = enqueue(conn, category=category, template_id=template_id, to_address=to, payload=payload or {},
                         idempotency_key=key, now=now)
    conn.commit()
    return message_id


def _row(conn, message_id):
    return dict(conn.execute("SELECT * FROM outbound_messages WHERE id = ?", (message_id,)).fetchone())


def _dispatch(conn, message_id, provider, now=NOW):
    status = dispatch_one(conn, message_id, provider=provider, now=now, app_origin=ORIGIN)
    conn.commit()
    return status


def test_enqueue_is_idempotent_and_in_the_callers_transaction(conn):
    first = _enqueue(conn)
    assert first is not None
    assert _enqueue(conn) is None
    enqueue(conn, category="SERVICE", template_id="auth.password_changed", to_address="b@example.com", payload={},
            idempotency_key="rolled-back", now=NOW)
    conn.rollback()  # the producing transaction failed: no message
    keys = [r[0] for r in conn.execute("SELECT idempotency_key FROM outbound_messages")]
    assert keys == ["k1"]


def test_unknown_templates_and_categories_are_refused(conn):
    with pytest.raises(ValueError):
        _enqueue(conn, "made.up")
    with pytest.raises(ValueError):
        _enqueue(conn, category="SPAM")


def test_enqueue_kicks_the_dispatch_job(conn):
    _enqueue(conn)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'outbox.dispatch'").fetchone()[0] == 1


def test_a_service_message_is_rendered_sent_and_its_secrets_redacted(conn):
    provider = FlakyProvider([])
    message_id = _enqueue(conn, "auth.password_reset", payload={"token": "tok-secret",
                                                                "reset_url": f"{ORIGIN}/reset?token=tok-secret"})
    assert _dispatch(conn, message_id, provider) == "SENT"
    row = _row(conn, message_id)
    assert (row["status"], row["provider"], row["provider_message_id"]) == ("SENT", "flaky", "msg-1")
    assert row["sent_at"] is not None and row["template_version"] == "v1"
    assert provider.sent[0]["idempotency_key"] == "k1"
    assert "tok-secret" not in row["payload_json"]  # a sent message keeps no live credential


def test_marketing_is_canceled(conn):
    provider = FlakyProvider([])
    message_id = _enqueue(conn, "announcement.service_notice", category="MARKETING")
    assert _dispatch(conn, message_id, provider) == "CANCELED"
    assert _row(conn, message_id)["last_error"] == "NO_CONSENT_OR_NOT_ENABLED"
    assert provider.sent == []


def test_a_complaint_suppresses_everything_including_service_mail(conn):
    suppress(conn, "Ada@Example.com", reason="COMPLAINT", now=NOW - timedelta(days=90))
    provider = FlakyProvider([])
    for key, template in (("a", "auth.verify_email"), ("b", "billing.payment_failed")):
        message_id = _enqueue(conn, template, key=key, payload={"token": "t", "verify_url": "u"})
        assert _dispatch(conn, message_id, provider) == "SUPPRESSED"
    assert provider.sent == []


def test_a_recent_hard_bounce_suppresses_everything(conn):
    suppress(conn, "ada@example.com", reason="HARD_BOUNCE", now=NOW - timedelta(days=29))
    message_id = _enqueue(conn, "auth.verify_email", payload={"token": "t", "verify_url": "u"})
    assert _dispatch(conn, message_id, FlakyProvider([])) == "SUPPRESSED"


def test_an_old_hard_bounce_still_lets_verify_and_reset_through_but_nothing_else(conn):
    suppress(conn, "ada@example.com", reason="HARD_BOUNCE", now=NOW - timedelta(days=31))
    provider = FlakyProvider([])
    verify = _enqueue(conn, "auth.verify_email", key="v", payload={"token": "t", "verify_url": "u"})
    reset = _enqueue(conn, "auth.password_reset", key="r", payload={"token": "t", "reset_url": "u"})
    other = _enqueue(conn, "billing.payment_failed", key="o")
    assert [_dispatch(conn, m, provider) for m in (verify, reset, other)] == ["SENT", "SENT", "SUPPRESSED"]


def test_transient_failures_back_off_then_fail(conn):
    assert TRANSIENT_BACKOFF_SECONDS == (60, 300, 1800, 7200, 21600)
    provider = FlakyProvider([TransientSendError("421 try later")] * 6)
    message_id = _enqueue(conn)
    now = NOW
    for delay in TRANSIENT_BACKOFF_SECONDS:
        assert _dispatch(conn, message_id, provider, now=now) == "QUEUED"
        row = _row(conn, message_id)
        assert datetime.fromisoformat(row["next_attempt_at"]) == now + timedelta(seconds=delay)
        now = now + timedelta(seconds=delay)
    assert _dispatch(conn, message_id, provider, now=now) == "FAILED"
    assert _row(conn, message_id)["attempts"] == 6


def test_a_permanent_failure_fails_at_once(conn):
    message_id = _enqueue(conn)
    assert _dispatch(conn, message_id, FlakyProvider([PermanentSendError("550 no such user")])) == "FAILED"
    assert "550" in _row(conn, message_id)["last_error"]


def test_claim_takes_due_rows_and_leases_them(conn):
    first = _enqueue(conn, key="a")
    _enqueue(conn, key="b", now=NOW)
    conn.execute("UPDATE outbound_messages SET next_attempt_at = ? WHERE idempotency_key = 'b'",
                 ((NOW + timedelta(hours=1)).isoformat(timespec="microseconds"),))
    conn.commit()
    assert claim_due(conn, now=NOW, limit=20) == [first]
    conn.commit()
    assert claim_due(conn, now=NOW, limit=20) == []  # leased: SENDING
    assert claim_due(conn, now=NOW + timedelta(minutes=6), limit=20) == [first]  # the lease lapsed


def test_the_console_provider_records_and_appends_to_the_log(tmp_path):
    provider = ConsoleEmailProvider(tmp_path / "outbox.log")
    message_id = provider.send(to="ada@example.com", subject="Hi", text="Body", html="<p>Body</p>",
                               idempotency_key="k", headers={"X-A": "1"})
    assert provider.sent[0]["to"] == "ada@example.com" and message_id
    line = json.loads((tmp_path / "outbox.log").read_text(encoding="utf-8").splitlines()[0])
    assert line["subject"] == "Hi" and line["text"] == "Body"


def test_the_smtp_adapter_uses_starttls_and_sets_the_headers(monkeypatch):
    import smtplib
    from webapp.comms.smtp import SmtpEmailProvider
    calls = []

    class Spy:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self, context=None):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, message):
            calls.append(("send", message["To"], message["From"], message["Subject"],
                          message["X-JobSearch-Idempotency-Key"], message["X-Custom"]))
            return {}

    monkeypatch.setattr(smtplib, "SMTP", Spy)
    provider = SmtpEmailProvider("smtp.example.test", 587, "user", "pw", True, "JobSearch <no-reply@example.test>")
    provider.send(to="ada@example.com", subject="Hello", text="Body", html="<p>Body</p>", idempotency_key="k-1",
                  headers={"X-Custom": "v"})
    names = [c[0] for c in calls]
    assert names.index("starttls") < names.index("login") < names.index("send")
    assert calls[-1][1:] == ("ada@example.com", "JobSearch <no-reply@example.test>", "Hello", "k-1", "v")


def test_smtp_errors_map_to_transient_and_permanent(monkeypatch):
    import smtplib
    from webapp.comms.smtp import SmtpEmailProvider

    def raising(exc):
        class Spy:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def ehlo(self):
                pass

            def starttls(self, context=None):
                pass

            def login(self, *a):
                pass

            def send_message(self, message):
                raise exc
        return Spy

    provider = SmtpEmailProvider("h", 587, None, None, True, "a@example.test")
    monkeypatch.setattr(smtplib, "SMTP", raising(smtplib.SMTPRecipientsRefused({"x": (550, b"no")})))
    with pytest.raises(PermanentSendError):
        provider.send(to="x", subject="s", text="t", html="h", idempotency_key="k", headers={})
    monkeypatch.setattr(smtplib, "SMTP", raising(smtplib.SMTPServerDisconnected("gone")))
    with pytest.raises(TransientSendError):
        provider.send(to="x", subject="s", text="t", html="h", idempotency_key="k", headers={})


def test_bounce_and_complaint_events_suppress_the_address_once(conn):
    assert record_email_event(conn, provider="p", provider_event_id="e1", kind="HARD_BOUNCE",
                              address="Ada@example.com", payload={}, now=NOW) is True
    assert record_email_event(conn, provider="p", provider_event_id="e1", kind="HARD_BOUNCE",
                              address="Ada@example.com", payload={}, now=NOW) is False  # redelivery
    conn.commit()
    row = conn.execute("SELECT reason FROM email_suppressions WHERE address_hash = ?",
                       (address_hash("ada@example.com"),)).fetchone()
    assert row[0] == "HARD_BOUNCE"


def test_the_outbox_dispatch_job_sends_through_the_configured_provider(tmp_path):
    from webapp.worker.handlers import default_handlers
    from webapp.worker.runner import Worker
    path = tmp_path / "db.sqlite3"
    init_db(path)
    settings = Settings(db_path=path, email_provider="console")
    conn = connect(path)
    _enqueue(conn, payload={})
    conn.close()
    Worker(settings, default_handlers(settings, providers_factory=lambda: None), clock=lambda: NOW,
           worker_id="w").run_once()
    conn = connect(path)
    try:
        assert conn.execute("SELECT status FROM outbound_messages").fetchone()[0] == "SENT"
    finally:
        conn.close()
    assert "Your password was changed" in (tmp_path / "outbox.log").read_text(encoding="utf-8")
