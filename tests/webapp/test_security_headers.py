"""Bundle 7 Task 6: security headers and a strict nonce-based CSP (spec §20.7, T4)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings

TEMPLATES = Path(__file__).resolve().parents[2] / "webapp" / "templates"
NONCE_ATTR = 'nonce="{{ request.state.csp_nonce }}"'
PAGES = ["/", "/profile", "/user-profile", "/discover", "/new-job", "/how-it-works", "/pairing",
         "/search-workspaces", "/walkthroughs"]
EXPECTED = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
}


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[1] / "fixtures" / "extensions")
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


def _csp_nonce(response) -> str:
    csp = response.headers["content-security-policy"]
    match = re.search(r"script-src 'self' 'nonce-([A-Za-z0-9+/=_-]+)'", csp)
    assert match, csp
    return match.group(1)


def test_every_template_script_carries_the_nonce_and_no_inline_handlers_or_styles():
    problems = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", text):
            if "src=" not in tag and NONCE_ATTR not in tag:
                problems.append(f"{path.name}: inline script without nonce: {tag}")
        for attr in re.findall(r"\son[a-z]+\s*=", text):
            problems.append(f"{path.name}: inline event handler {attr.strip()}")
        if re.search(r"\sstyle\s*=", text):
            problems.append(f"{path.name}: inline style attribute")
    assert problems == []


@pytest.mark.parametrize("page", PAGES)
def test_pages_send_a_strict_csp_whose_nonce_matches_the_rendered_scripts(client, page):
    response = client.get(page)
    assert response.status_code in (200, 303, 307), (page, response.status_code)
    if response.status_code != 200:
        return
    nonce = _csp_nonce(response)
    csp = response.headers["content-security-policy"]
    for directive in ("default-src 'self'", "style-src 'self'", "img-src 'self' data:", "connect-src 'self'",
                      "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'"):
        assert directive in csp
    assert "unsafe-inline" not in csp
    for tag in re.findall(r"<script\b[^>]*>", response.text):
        if "src=" not in tag:
            assert f'nonce="{nonce}"' in tag, tag


def test_nonce_differs_per_request(client):
    assert _csp_nonce(client.get("/")) != _csp_nonce(client.get("/"))


def test_fixed_security_headers_are_exact_and_hsts_is_hosted_only(client):
    response = client.get("/health")
    for name, value in EXPECTED.items():
        assert response.headers[name] == value
    assert "strict-transport-security" not in response.headers


def test_hosted_mode_adds_hsts():
    from webapp.security_middleware import SecurityHeadersMiddleware

    captured = {}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        if message["type"] == "http.response.start":
            captured.update({k.decode(): v.decode() for k, v in message["headers"]})

    import asyncio

    middleware = SecurityHeadersMiddleware(app, hosted=True)
    asyncio.run(middleware({"type": "http", "path": "/", "headers": [], "state": {}}, None, send))
    assert captured["strict-transport-security"] == "max-age=31536000; includeSubDomains"
