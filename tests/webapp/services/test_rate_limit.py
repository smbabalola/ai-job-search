"""Bundle 7 spec A8: database-backed fixed-window rate limits."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import publish_legal_documents, sign_up
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect, init_db
from webapp.services.rate_limit import RATE_LIMITS, hit

NOW = datetime(2026, 10, 1, 9, 0, 30, tzinfo=timezone.utc)


def test_rate_limits_are_the_spec_values():
    assert RATE_LIMITS == {
        "signup": (5, 3600), "login": (10, 900), "password_reset": (5, 3600), "verify_resend": (5, 3600),
        "pairing_code": (10, 3600), "token_refresh": (60, 3600), "ai_start": (30, 60),
    }


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    return path


def test_fixed_window_allows_the_limit_then_refuses_until_the_window_rolls(db):
    conn = connect(db)
    decisions = [hit(conn, key="signup:1.2.3.4", limit=5, window_seconds=3600, now=NOW) for _ in range(6)]
    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert 0 < decisions[-1].retry_after <= 3600
    later = NOW.replace(minute=0, second=0) + timedelta(hours=1)
    assert hit(conn, key="signup:1.2.3.4", limit=5, window_seconds=3600, now=later).allowed
    assert hit(conn, key="signup:5.6.7.8", limit=5, window_seconds=3600, now=NOW).allowed  # keys are independent
    conn.close()


def test_concurrent_hits_allow_exactly_the_limit(db):
    results, barrier = [], threading.Barrier(10)

    def worker():
        conn = connect(db)
        try:
            barrier.wait()
            results.append(hit(conn, key="login:x", limit=4, window_seconds=900, now=NOW).allowed)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 6 + [True] * 4


def test_the_sixth_signup_from_one_address_is_rate_limited(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[2] / "fixtures" / "extensions",
                        auth_required_in_local=True)
    with TestClient(create_app(settings)) as client:
        publish_legal_documents(settings)
        statuses = [sign_up(client, email=f"user{i}@example.com").status_code for i in range(6)]
        assert statuses[:5] == [200] * 5
        assert statuses[5] == 429
        limited = sign_up(client, email="user9@example.com")
        assert "RATE_LIMITED" in limited.text and int(limited.headers["retry-after"]) > 0
