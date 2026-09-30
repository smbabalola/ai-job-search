"""Bundle 7 spec §11.5: the safety and account surfaces are never gated. An
account whose effective plan has every feature off and every allowance 0,
with its subscription PAST_DUE beyond grace, still reaches each of them (any
status but 402), and none of them consults the gate.

Notifications, preferences, export and deletion join this list with the tasks
that build them (20, 23, 28)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from product.entitlements import ALLOWANCES, FEATURES
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect
from webapp.services.entitlements import EntitlementGate
from webapp.services.ownership import AccountScope
from webapp.services.usage import UsageService
from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.factories import build_account_graph

PLANS = Path(__file__).parents[2] / "product" / "plans"


def _nothing_catalog(tmp_path) -> Path:
    doc = json.loads((PLANS / "plan-catalog.dev.json").read_text(encoding="utf-8"))
    doc["catalog_version"] = "test-nothing"
    for plan in doc["plans"].values():
        plan["features"] = {feature: False for feature in FEATURES}
        plan["allowances"] = {allowance: {"limit": 0, "window": "period"} for allowance in ALLOWANCES}
    path = tmp_path / "plan-catalog.nothing.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    gate_calls: list[str] = []
    real_feature, real_reserve = EntitlementGate.require_feature, UsageService.reserve

    def require_feature(self, conn, scope, feature, *, now):
        gate_calls.append(feature)
        return real_feature(self, conn, scope, feature, now=now)

    def reserve(self, conn, scope, *, allowance, **kwargs):
        gate_calls.append(allowance)
        return real_reserve(self, conn, scope, allowance=allowance, **kwargs)

    monkeypatch.setattr(EntitlementGate, "require_feature", require_feature)
    monkeypatch.setattr(UsageService, "reserve", reserve)
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[1] / "fixtures" / "extensions",
                        auth_required_in_local=True, plan_catalog_path=_nothing_catalog(tmp_path))
    with TestClient(create_app(settings)) as client:
        publish_legal_documents(settings)
        sign_up_and_verify(client)
        sign_in(client)
        account_id = client.get("/auth/me").json()["account_id"]
        conn = connect(settings)
        graph = build_account_graph(conn, account_id=account_id, documents_root=settings.documents_root)
        # a real paid subscription through the fake provider, then PAST_DUE for a year: far beyond any grace
        scope = AccountScope(account_id=account_id, profile_root=tmp_path)
        service = client.app.state.billing_service
        url = service.start_checkout(conn, scope, plan_id="pro", interval="month", now=datetime.now(timezone.utc))
        conn.commit()
        service.provider.simulate(url.rstrip("/").split("/")[-1], "pay")
        assert conn.execute("UPDATE subscriptions SET state = 'PAST_DUE', past_due_since = ? WHERE account_id = ?",
                            ("2025-09-15T00:00:00+00:00", account_id)).rowcount == 1
        conn.commit()
        conn.close()
        gate_calls.clear()
        yield client, graph["ids"], gate_calls


def test_the_account_really_has_nothing(world):
    client, ids, _ = world
    usage = client.get("/api/usage").json()
    assert usage["plan_id"] == "free"
    assert all(row["limit"] == 0 for row in usage["usage"])
    response = client.post(f"/api/workspaces/{ids['workspace_id']}/understand", json={"request_id": "r"},
                           headers={"X-CSRF-Token": csrf_token(client)})
    assert response.status_code == 402 and response.json()["error"] == "FEATURE_NOT_IN_PLAN"


def test_every_never_gated_surface_still_answers(world):
    client, ids, gate_calls = world
    ws = ids["workspace_id"]
    reads = [f"/api/workspaces/{ws}", f"/api/workspaces/{ws}/application-documents", "/api/usage",
             f"/api/workspaces/{ws}/submit/state"]
    writes = [
        (f"/api/workspaces/{ws}/review-decisions", {}),                      # review
        (f"/api/workspaces/{ws}/review/approve", {}),                        # approve
        (f"/api/workspaces/{ws}/review/revoke", {}),                         # revoke
        (f"/api/workspaces/{ws}/review/fields/salary/disposition", {}),      # field dispositions
        (f"/api/workspaces/{ws}/submit/attempts/attempt_x/resolve", {}),     # ambiguity resolution
        (f"/api/workspaces/{ws}/submit/cancel", {"authorization_id": "x"}),  # cancel before dispatch
        ("/api/autonomy/kill-switch/engage", {}),                            # kill switch
        ("/api/autonomy/pause", {}),                                         # autonomy off / pause
        ("/api/settings/devices/device_x/revoke", {}),                       # device revocation
        ("/api/billing/portal", {}),                                         # manage billing
        ("/api/billing/cancel", {}),                                         # cancel
    ]
    statuses = {}
    for url in reads:
        statuses[url] = client.get(url).status_code
    for url, body in writes:
        statuses[url] = client.post(url, json=body, headers={"X-CSRF-Token": csrf_token(client)}).status_code
    statuses["/auth/logout"] = client.post("/auth/logout", headers={"X-CSRF-Token": csrf_token(client)},
                                           follow_redirects=False).status_code  # last: it ends the session
    gated = {url: status for url, status in statuses.items() if status == 402}
    assert gated == {}, statuses
    assert all(status < 500 for status in statuses.values()), statuses
    assert gate_calls == []
