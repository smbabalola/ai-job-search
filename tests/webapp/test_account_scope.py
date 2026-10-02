"""Bundle 7 spec §6.2, A3, A7: the account scope comes from the signed-in session."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import csrf_token, post, publish_legal_documents, sign_in, sign_up, sign_up_and_verify
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect


@pytest.fixture
def world(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[1] / "fixtures" / "extensions",
                        auth_required_in_local=True)
    with TestClient(create_app(settings)) as client:
        publish_legal_documents(settings)
        yield client, settings


def test_signed_out_api_calls_are_401_and_pages_redirect_to_login(world):
    client, _ = world
    api = client.get("/api/search-workspaces")
    assert api.status_code == 401 and api.json()["error"] == "SIGN_IN_REQUIRED"
    page = client.get("/profile", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/login")


def test_unverified_users_are_refused_with_email_not_verified(world):
    client, _ = world
    sign_up(client)
    sign_in(client)
    response = post(client, "/api/search-workspaces", data=None, json={"name": "Blocked"})
    assert response.status_code == 403 and response.json()["error"] == "EMAIL_NOT_VERIFIED"
    page = client.get("/profile", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/check-email"


def test_the_scope_is_the_signed_in_users_own_account(world):
    client, settings = world
    sign_up_and_verify(client)
    sign_in(client)
    me = client.get("/auth/me").json()
    created = post(client, "/api/search-workspaces", json={"name": "Mine"})
    assert created.status_code == 201
    conn = connect(settings.db_path)
    owner = conn.execute("SELECT account_id FROM search_workspaces WHERE id = ?", (created.json()["search_workspace"]["id"],)).fetchone()
    conn.close()
    assert owner["account_id"] == me["account_id"] != "account_local"


def test_suspended_accounts_are_refused(world):
    client, settings = world
    sign_up_and_verify(client)
    sign_in(client)
    account_id = client.get("/auth/me").json()["account_id"]
    conn = connect(settings.db_path)
    conn.execute("UPDATE accounts SET status = 'SUSPENDED' WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()
    response = client.get("/api/search-workspaces")
    assert response.status_code == 403 and response.json()["error"] == "ACCOUNT_SUSPENDED"


def test_the_local_account_is_never_reachable_in_auth_mode(world):
    client, _ = world
    sign_up_and_verify(client)
    sign_in(client)
    listed = client.get("/api/search-workspaces").json()
    assert all(w["id"] != "search_default" for w in listed.get("search_workspaces", listed if isinstance(listed, list) else []))
    assert client.get("/api/search-workspaces/search_default").status_code == 404
