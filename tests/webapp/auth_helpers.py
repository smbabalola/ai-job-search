"""Shared helpers for tests that run with real sign-in (auth_required_in_local)."""
from __future__ import annotations

import re

from webapp import comms
from webapp.persistence.db import connect

PASSWORD = "correct horse battery staple"


def publish_legal_documents(settings) -> None:
    conn = connect(settings)
    try:
        conn.execute("INSERT INTO legal_documents (id, kind, version, published_at, content_sha256) "
                     "VALUES ('terms_v1', 'TERMS', '1', '2026-09-01T00:00:00+00:00', ?) ON CONFLICT DO NOTHING",
                     ("a" * 64,))
        conn.execute("INSERT INTO legal_documents (id, kind, version, published_at, content_sha256) "
                     "VALUES ('privacy_v1', 'PRIVACY', '1', '2026-09-01T00:00:00+00:00', ?) ON CONFLICT DO NOTHING",
                     ("b" * 64,))
        conn.commit()
    finally:
        conn.close()


def csrf_token(client) -> str:
    """The token the next unsafe request must carry (pre-session or session)."""
    return client.get("/auth/csrf").json()["csrf_token"]


def post(client, url, data=None, **kwargs):
    headers = {"X-CSRF-Token": csrf_token(client), **kwargs.pop("headers", {})}
    return client.post(url, data=data, headers=headers, **kwargs)


def mail(template_id, to=None):
    return [m for m in comms.outbox_for_tests() if m["template_id"] == template_id
            and (to is None or m["to_address"] == to)]


def token_from_mail(template_id, to=None) -> str:
    return mail(template_id, to)[-1]["payload"]["token"]


def sign_up(client, email="ada@example.com", password=PASSWORD, name="Ada Lovelace"):
    return post(client, "/auth/signup", data={"email": email, "password": password, "display_name": name,
                                              "accept_terms": "terms_v1", "accept_privacy": "privacy_v1"})


def sign_up_and_verify(client, email="ada@example.com", password=PASSWORD, name="Ada Lovelace"):
    sign_up(client, email=email, password=password, name=name)
    post(client, "/auth/verify-email", data={"token": token_from_mail("auth.verify_email", email.strip().lower())})


def sign_in(client, email="ada@example.com", password=PASSWORD):
    return post(client, "/auth/login", data={"email": email, "password": password}, follow_redirects=False)


def meta_csrf(html: str) -> str | None:
    match = re.search(r'<meta name="csrf-token" content="([^"]*)"', html)
    return match.group(1) if match else None
