"""Bundle 7 Task 25 (spec §12, §13.3, §21.3): pricing, plan selection, billing,
usage and account settings pages; the header meter and banner; cost
disclosure; human renderings of every refusal code."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect


@pytest.fixture
def world(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        auth_required_in_local=True)
    app = create_app(settings)
    with TestClient(app) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client)
        yield app, client, settings, client.get("/auth/me").json()["account_id"]


def _post(client, url, **kwargs):
    return client.post(url, headers={"X-CSRF-Token": csrf_token(client)}, follow_redirects=False, **kwargs)


def test_pricing_renders_the_catalog_matrix_with_unresolved_prices_as_a_dash(world):
    _, client, _, _ = world
    page = TestClient(client.app).get("/pricing")  # public: no session
    assert page.status_code == 200
    for plan in ("Free", "Pro", "Power"):
        assert plan in page.text
    assert "—" in page.text  # the dev catalog resolves no display prices (DP-3)
    assert "Tailored CVs" in page.text  # a feature row
    assert "ai.cost_micro_usd" not in page.text and "fair-use" not in page.text.lower()  # hidden allowance


def test_choosing_free_makes_no_provider_call_and_goes_to_the_dashboard(world):
    app, client, _, _ = world
    calls = []
    real = app.state.billing_service.provider.create_checkout
    app.state.billing_service.provider.create_checkout = lambda **kw: calls.append(kw) or real(**kw)
    chosen = _post(client, "/plans/choose", data={"plan_id": "free"})
    assert chosen.status_code == 303 and chosen.headers["location"] == "/"
    assert calls == []


def test_choosing_pro_goes_through_the_dev_checkout_and_the_return_page_sees_the_subscription(world):
    app, client, _, _ = world
    chosen = _post(client, "/plans/choose", data={"plan_id": "pro"})
    assert chosen.status_code == 303 and "/dev/billing/checkout/" in chosen.headers["location"]
    session_id = chosen.headers["location"].rsplit("/", 1)[-1]
    paid = client.post(f"/dev/billing/checkout/{session_id}/pay", headers={"X-CSRF-Token": csrf_token(client)},
                       follow_redirects=False)
    assert paid.status_code in (302, 303)
    back = paid.headers["location"]
    assert "/settings/billing?checkout=" in back
    page = client.get(back.split("://", 1)[-1][back.split("://", 1)[-1].index("/"):])
    assert page.status_code == 200 and "data-checkout-polling" in page.text
    status = client.get("/api/billing/status").json()
    assert status["plan_id"] == "pro" and status["state"] in ("TRIALING", "ACTIVE")


def test_the_usage_page_matches_the_summary_and_hides_the_ceiling(world):
    _, client, _, _ = world
    summary = client.get("/api/usage").json()["usage"]
    page = client.get("/settings/usage")
    assert page.status_code == 200
    for row in summary:
        assert f'data-allowance="{row["allowance"]}"' in page.text
        assert f'data-used="{row["used"]}"' in page.text
    assert "ai.cost_micro_usd" not in page.text


def test_the_header_meter_and_plan_badge(world):
    _, client, _, _ = world
    page = client.get("/cvs")
    assert "data-plan-badge" in page.text and "data-prepare-meter" in page.text


def test_the_past_due_banner(world):
    _, client, settings, account_id = world
    conn = connect(settings)
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO subscriptions (id, account_id, provider, provider_subscription_id, plan_id, catalog_version, "
        "interval, state, current_period_start, current_period_end, cancel_at_period_end, past_due_since, "
        "snapshot_json, snapshot_hash, updated_at) VALUES ('sub_1', ?, 'fake', 'psub_1', 'pro', ?, 'month', "
        "'PAST_DUE', ?, ?, 0, ?, '{}', 'h', ?)",
        (account_id, client.app.state.billing_service.catalog.catalog_version, (now - timedelta(days=5)).isoformat(),
         (now + timedelta(days=25)).isoformat(), (now - timedelta(days=1)).isoformat(), now.isoformat()))
    conn.commit()
    conn.close()
    page = client.get("/settings/billing")
    assert "data-past-due-banner" in page.text


def test_the_prepare_cost_is_disclosed_on_the_workspace(world):
    from tests.webapp.api.test_workspace_routes import _source_record
    _, client, _, _ = world
    ws = client.post("/api/workspaces", json={"company": "Acme", "title": "Backend Engineer",
                                              "source_record": _source_record(), "source_record_origin": "manual_entry"},
                     headers={"X-CSRF-Token": csrf_token(client)}).json()["workspace"]["id"]
    page = client.get(f"/workspaces/{ws}")
    assert re.search(r"Uses 1 of your \d+ remaining prepares this period", page.text)


@pytest.mark.parametrize("path", ["/settings", "/settings/account", "/settings/security", "/settings/sessions",
                                  "/settings/devices", "/settings/billing", "/plans"])
def test_every_settings_page_renders(world, path):
    _, client, _, _ = world
    page = client.get(path)
    assert page.status_code == 200, path


def test_every_refusal_code_renders_a_human_message_and_an_action_for_pages():
    from webapp.api.errors import ERROR_CODES, ERROR_PAGE_ACTIONS, render_error_page
    assert set(ERROR_PAGE_ACTIONS) == set(ERROR_CODES)
    for code in ERROR_CODES:
        html = render_error_page(code, "Something to tell you.")
        assert "Something to tell you." in html and "<a " in html and code not in html.split("<title>")[0]


def test_a_page_request_gets_html_and_an_api_request_gets_json(world):
    from tests.webapp.api.test_workspace_routes import _source_record
    _, client, _, _ = world
    headers = {"X-CSRF-Token": csrf_token(client)}
    ws = client.post("/api/workspaces", json={"company": "Acme", "title": "Backend Engineer",
                                              "source_record": _source_record(), "source_record_origin": "manual_entry"},
                     headers=headers).json()["workspace"]["id"]
    api = client.post(f"/api/workspaces/{ws}/understand", json={"request_id": "r"}, headers=headers)
    assert api.status_code == 409 and api.json()["error"] == "ONBOARDING_INCOMPLETE"
    from webapp.api.errors import error_response_for
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/workspaces/x", "headers": [(b"accept", b"text/html")]}
    page = error_response_for(Request(scope), "ONBOARDING_INCOMPLETE", "Finish setting up.", 409, detail={})
    assert page.status_code == 409 and b"/onboarding" in page.body and page.media_type == "text/html"


def test_pages_with_their_own_status_render_for_a_signed_in_account(tmp_path):
    """The header's plan/usage block must not shadow a page's own ``status``
    (the Submit Review and fill pages render one)."""
    from fastapi.testclient import TestClient
    from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
    from webapp.app import create_app
    from webapp.config import Settings
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        auth_required_in_local=True)
    with TestClient(create_app(settings)) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client)
        ws = client.post("/api/workspaces", headers={"X-CSRF-Token": csrf_token(client)}, json={
            "company": "Acme", "title": "Engineer", "source_record_origin": "manual_entry",
            "source_record": {"schema_version": "job-source-record.v0", "source": "manual",
                              "captured_at": "2026-10-01T00:00:00Z", "company": "Acme", "title": "Engineer",
                              "description": "Python."}}).json()["workspace"]["id"]
        for path in (f"/workspaces/{ws}/submit", f"/workspaces/{ws}/fill-plan", f"/workspaces/{ws}"):
            page = client.get(path)
            assert page.status_code == 200, (path, page.text[:300])
            assert "data-plan-badge" in page.text, path  # the header still renders
