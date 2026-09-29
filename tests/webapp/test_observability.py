"""Bundle 7 Task 6: request ids, redacted JSON logs, safe 500s (spec §20.6)."""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings
from webapp.observability import JsonFormatter, LogErrorReporter, redact


@pytest.fixture
def app(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[1] / "fixtures" / "extensions")
    application = create_app(settings)

    @application.get("/__boom")
    def boom():
        raise RuntimeError("secret internal detail Traceback-ish")

    return application


def test_request_id_is_generated_echoed_and_validated(app):
    with TestClient(app) as client:
        generated = client.get("/health").headers["x-request-id"]
        uuid.UUID(generated)
        supplied = str(uuid.uuid4())
        assert client.get("/health", headers={"X-Request-ID": supplied}).headers["x-request-id"] == supplied
        replaced = client.get("/health", headers={"X-Request-ID": "not-a-uuid"}).headers["x-request-id"]
        assert replaced != "not-a-uuid"
        uuid.UUID(replaced)


def test_unhandled_errors_return_a_request_id_and_no_internal_detail(app):
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__boom", headers={"Accept": "application/json"})
        assert response.status_code == 500
        body = response.json()
        assert body == {"error": "INTERNAL_ERROR", "message": "Something went wrong.",
                        "request_id": response.headers["x-request-id"]}
        assert "secret internal detail" not in response.text
        html = client.get("/__boom", headers={"Accept": "text/html"})
        assert html.status_code == 500 and response.headers["x-request-id"] != html.headers["x-request-id"]
        assert html.headers["x-request-id"] in html.text and "secret internal detail" not in html.text


def test_unhandled_errors_reach_the_error_reporter(app):
    reported = []
    app.state.error_reporter = type("R", (), {"report": lambda self, exc, context: reported.append((exc, context))})()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__boom")
    assert isinstance(reported[0][0], RuntimeError)
    assert reported[0][1]["request_id"] == response.headers["x-request-id"]


def test_each_request_logs_one_json_line(app, caplog):
    with caplog.at_level(logging.INFO, logger="webapp.request"):
        with TestClient(app) as client:
            response = client.get("/health")
    records = [r for r in caplog.records if r.name == "webapp.request"]
    assert len(records) == 1
    line = json.loads(JsonFormatter().format(records[0]))
    assert line["request_id"] == response.headers["x-request-id"]
    assert (line["method"], line["route"], line["status"]) == ("GET", "/health", 200)
    assert isinstance(line["duration_ms"], (int, float))


def test_redact_masks_secret_keys_recursively_and_case_insensitively():
    value = {"Authorization": "Bearer x", "nested": [{"refresh_token": "y", "ok": 1}],
             "Cookie": "a=b", "password": "p", "CODE": "123456", "keep": "visible"}
    assert redact(value) == {"Authorization": "[REDACTED]", "nested": [{"refresh_token": "[REDACTED]", "ok": 1}],
                             "Cookie": "[REDACTED]", "password": "[REDACTED]", "CODE": "[REDACTED]", "keep": "visible"}


def test_json_formatter_redacts_extra_fields():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    record.detail = {"access_token": "t"}
    assert json.loads(JsonFormatter().format(record))["detail"] == {"access_token": "[REDACTED]"}


def test_log_error_reporter_logs_without_raising(caplog):
    with caplog.at_level(logging.ERROR, logger="webapp.errors"):
        LogErrorReporter().report(RuntimeError("x"), {"request_id": "r"})
    assert any("r" in r.getMessage() for r in caplog.records)
