"""Bundle 7 spec A5, T3: CSRF protection for cookie-authenticated requests."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import PASSWORD, csrf_token, publish_legal_documents, sign_up_and_verify
from webapp.app import create_app
from webapp.config import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[2] / "fixtures" / "extensions",
                        auth_required_in_local=True)
    with TestClient(create_app(settings)) as c:
        publish_legal_documents(settings)
        yield c


def test_pages_expose_a_token_and_set_a_presession_cookie(client):
    page = client.get("/login")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text).group(1)
    assert token and "js_presession" in page.headers.get("set-cookie", "")
    assert f'name="csrf_token" value="{token}"' in page.text  # every form carries it


def test_a_public_form_post_without_a_token_is_refused(client):
    client.get("/login")
    response = client.post("/auth/login", data={"email": "a@example.com", "password": "x"})
    assert response.status_code == 403 and response.json()["error"] == "CSRF_FAILED"


def test_a_presession_token_for_another_browser_is_refused(client):
    token = csrf_token(client)
    other = TestClient(client.app)
    other.get("/login")
    response = other.post("/auth/login", data={"email": "a@example.com", "password": "x", "csrf_token": token})
    assert response.status_code == 403


def test_a_valid_token_passes_as_form_field_or_header(client):
    token = csrf_token(client)
    assert client.post("/auth/login", data={"email": "a@example.com", "password": "x",
                                             "csrf_token": token}).status_code == 401
    assert client.post("/auth/login", data={"email": "a@example.com", "password": "x"},
                       headers={"X-CSRF-Token": token}).status_code == 401


def test_a_cross_origin_post_is_refused_even_with_a_token(client):
    token = csrf_token(client)
    response = client.post("/auth/login", data={"email": "a@example.com", "password": "x", "csrf_token": token},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    same = client.post("/auth/login", data={"email": "a@example.com", "password": "x", "csrf_token": token},
                       headers={"Origin": "http://testserver"})
    assert same.status_code == 401


def test_signed_in_requests_need_the_session_token(client):
    sign_up_and_verify(client)
    stale = csrf_token(client)  # read before login: the pre-session token
    client.post("/auth/login", data={"email": "ada@example.com", "password": PASSWORD, "csrf_token": stale})
    assert client.post("/auth/logout", headers={"X-CSRF-Token": stale}).status_code == 403
    session_token = csrf_token(client)
    assert session_token != stale
    assert client.post("/auth/logout", headers={"X-CSRF-Token": session_token},
                       follow_redirects=False).status_code == 303


def test_bearer_requests_without_cookies_are_exempt(client):
    fresh = TestClient(client.app)
    response = fresh.post("/auth/logout", headers={"Authorization": "Bearer abc"}, follow_redirects=False)
    assert response.status_code == 303


def test_local_mode_without_auth_keeps_its_behaviour(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[2] / "fixtures" / "extensions")
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/search-workspaces", json={"name": "No token needed"}).status_code in (200, 201)
