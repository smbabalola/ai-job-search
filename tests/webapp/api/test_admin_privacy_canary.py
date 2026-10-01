"""Bundle 7 spec §19.4 (Task 27): the admin console never shows candidate
content. A canary is planted in the content tables (profile sources,
document filenames, artifact payloads, answer values, job text, notes, CV
library text, notifications); no admin page or API response may carry it."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from webapp.api.admin_api import ADMIN_ENDPOINTS
from webapp.app import create_app
from webapp.persistence.db import connect
from tests.webapp.admin_helpers import admin_settings, customer, make_staff, staff_login
from tests.webapp.factories import build_account_graph

CANARY = "CANARY-7f3e"


@pytest.fixture
def world(tmp_path):
    settings = admin_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app):
        client = customer(app)
        account_id = client.get("/auth/me").json()["account_id"]
        conn = connect(settings)
        try:
            graph = build_account_graph(conn, account_id=account_id, documents_root=settings.documents_root)
            _plant_more(conn, settings, account_id)
            conn.commit()
            planted = _planted_columns(conn, graph["canary"])
        finally:
            conn.close()
        yield app, settings, account_id, graph["canary"], planted


def _plant_more(conn, settings, account_id):
    """Content the account graph does not write: a profile source, an approved answer value."""
    from webapp.persistence.autonomy_answers import approve_answer
    from webapp.storage.profile_sources import profile_source_store_from_settings
    from webapp.persistence.identity import CANDIDATE_SOURCE
    profile_source_store_from_settings(settings).for_account(conn, account_id).write(
        CANDIDATE_SOURCE, f"# {CANARY} candidate profile\n")
    approve_answer(conn, account_id=account_id, subject="contact.phone", value=f"{CANARY} +44 7700 900000",
                   reach="ACCOUNT", scope_id=None, context={}, basis={"kind": "USER_ASSERTION"}, approved_by="user",
                   now=datetime.now(timezone.utc), commit=False)


CONTENT_COLUMNS = (
    ("profile_source_revisions", "content"), ("application_document_versions", "original_filename"),
    ("artifacts", "payload_json"), ("approved_answers", "value_json"), ("workspaces", "title"),
    ("workflow_events", "note"), ("cv_library_items", "title"), ("cv_library_versions", "note"),
    ("notifications", "detail_json"), ("user_profile_versions", "payload_json"),
)


def _planted_columns(conn, graph_canary) -> set[tuple[str, str]]:
    found = set()
    for table, column in CONTENT_COLUMNS:
        for marker in (CANARY, graph_canary):
            if conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?", (f"%{marker}%",)).fetchone()[0]:
                found.add((table, column))
    return found


def _bodies(app, account_id) -> dict[str, str]:
    client = staff_login(app, make_staff(app.state.settings, "ADMIN"))
    pages = ["/admin", "/admin/accounts", f"/admin/accounts?q={account_id}", f"/admin/accounts/{account_id}",
             "/admin/billing/events", "/admin/jobs", "/admin/outbox", "/admin/announcements", "/admin/controls",
             "/admin/staff", "/admin/audit"]
    api = [path.replace("{account_id}", account_id) for method, path, _ in ADMIN_ENDPOINTS
           if method == "GET"]
    out = {}
    for url in pages + api:
        response = client.get(url)
        assert response.status_code == 200, (url, response.status_code)
        out[url] = response.text
    return out


def test_no_admin_response_contains_candidate_content(world):
    app, settings, account_id, graph_canary, planted = world
    assert planted == set(CONTENT_COLUMNS), f"the canary is not in every content column: {set(CONTENT_COLUMNS) - planted}"
    for url, body in _bodies(app, account_id).items():
        assert CANARY not in body and graph_canary not in body, url


def test_the_account_page_still_shows_metadata(world):
    app, settings, account_id, graph_canary, planted = world
    body = _bodies(app, account_id)[f"/admin/accounts/{account_id}"]
    assert "ada@example.com" in body and "data-admin-account" in body
