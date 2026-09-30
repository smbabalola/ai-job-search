"""Bundle 7 spec §10.6, T1: no route lets one account read or change another's data.

Account B, signed in (and with its own extension credential), calls every
USER and EXTENSION route with account A's ids substituted into the path. For
every call: no A canary string appears in the response, and A's rows are
byte-identical before and after the whole walk. A negative control proves the
walk catches a route that ignores ownership.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.factories import build_account_graph
from webapp.api.route_classes import route_class_of
from webapp.app import create_app
from webapp.config import Settings
from webapp.persistence.db import connect
from webapp.persistence.tenancy import TENANT_TABLES, account_rows_sql

# (method, path) -> reason. At most five, each with a reason (spec §10.6).
OPT_OUTS: dict[tuple[str, str], str] = {}
FALLBACK_IDS = {"run_id": "run_absent", "attempt_id": "attempt_absent", "action_index": "0", "plan_id": "plan_absent",
                "delta_id": "delta_absent", "proposal_id": "proposal_absent", "candidate_id": "cand_absent",
                "entry_id": "entry_absent", "exception_id": "exc_absent", "answer_key": "subject:contact.email",
                "walkthrough_id": "dashboard", "source_path": "CLAUDE.md"}
PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")


def _snapshot(conn, account_id: str) -> str:
    digest = hashlib.sha256()
    for table, spec in sorted(TENANT_TABLES.items()):
        if spec.owner == "GLOBAL":
            continue
        sql, _ = account_rows_sql(table)
        rows = sorted(json.dumps([str(v) for v in tuple(r)]) for r in conn.execute(sql, (account_id,)).fetchall())
        digest.update(f"{table}:{rows}".encode())
    return digest.hexdigest()


def probe_every_route(app, b_client, graph_a, graph_b):
    """Walk every USER/EXTENSION route with A's ids as B. Returns (leaks, probed, statuses)."""
    b_client.headers["X-CSRF-Token"] = csrf_token(b_client)
    leaks, probed, statuses = [], 0, {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        klass = route_class_of(route)[0]
        if klass not in ("USER", "EXTENSION"):
            continue
        names = PARAM.findall(route.path)
        if not names:
            continue  # routes without ids only ever see the caller's own data
        values = {name: str(graph_a["ids"].get(name, FALLBACK_IDS.get(name, f"{name}_absent"))) for name in names}
        path = PARAM.sub(lambda m: values[m.group(1)], route.path)
        headers = {"X-Handoff-Credential": graph_b["extension_secret"]} if klass == "EXTENSION" else {}
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            if (method, route.path) in OPT_OUTS:
                continue
            kwargs = {"headers": headers}
            if method != "GET":
                kwargs["json"] = {}
            response = b_client.request(method, path, **kwargs)
            probed += 1
            statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
            if graph_a["canary"] in response.text:
                leaks.append(f"{method} {route.path} -> {response.status_code} leaked A's data")
            if 200 <= response.status_code < 300 and method != "GET":
                leaks.append(f"{method} {route.path} -> {response.status_code} accepted a write on A's ids")
    return leaks, probed, statuses


@pytest.fixture
def world(tmp_path):
    settings = Settings(db_path=tmp_path / "db.sqlite3", documents_root=tmp_path / "documents",
                        extensions_dir=Path(__file__).parents[1] / "fixtures" / "extensions",
                        auth_required_in_local=True)
    app = create_app(settings)
    with TestClient(app) as a_client, TestClient(app) as b_client:
        publish_legal_documents(settings)
        for client, email in ((a_client, "a@example.com"), (b_client, "b@example.com")):
            sign_up_and_verify(client, email=email)
            sign_in(client, email=email)
        a_account = a_client.get("/auth/me").json()["account_id"]
        b_account = b_client.get("/auth/me").json()["account_id"]
        conn = connect(settings)
        graph_a = build_account_graph(conn, account_id=a_account, documents_root=settings.documents_root)
        graph_b = build_account_graph(conn, account_id=b_account, documents_root=settings.documents_root)
        conn.close()
        yield app, settings, b_client, graph_a, graph_b, a_account


def test_account_b_cannot_read_or_change_account_a_through_any_route(world):
    app, settings, b_client, graph_a, graph_b, a_account = world
    assert len(OPT_OUTS) <= 5
    conn = connect(settings)
    before = _snapshot(conn, a_account)
    conn.close()
    leaks, probed, statuses = probe_every_route(app, b_client, graph_a, graph_b)
    conn = connect(settings)
    after = _snapshot(conn, a_account)
    conn.close()
    assert probed > 50
    # The probes reached real handlers: most are refused by ownership checks
    # (404), not by auth or CSRF (401/403 on every call would prove nothing).
    assert statuses.get(404, 0) >= probed // 3, statuses
    assert statuses.get(403, 0) + statuses.get(401, 0) < probed // 3, statuses
    assert leaks == [], "\n".join(leaks)
    assert after == before, "a route changed account A's rows"


def test_the_harness_catches_a_route_that_ignores_ownership(world):
    """Negative control: a deliberately unscoped route must be reported."""
    from fastapi import Depends

    from webapp.api.route_classes import USER

    app, settings, b_client, graph_a, graph_b, a_account = world

    @app.get("/api/workspaces/{workspace_id}/__leaky", dependencies=[Depends(USER)])
    def leaky(workspace_id: str):
        conn = connect(settings)
        try:
            row = conn.execute("SELECT company FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            return {"company": row["company"] if row else None}
        finally:
            conn.close()

    leaks, _, _ = probe_every_route(app, b_client, graph_a, graph_b)
    assert any("__leaky" in leak for leak in leaks), leaks


def test_account_b_still_reaches_its_own_data(world):
    app, settings, b_client, graph_a, graph_b, a_account = world
    own = b_client.get(f"/api/workspaces/{graph_b['ids']['workspace_id']}")
    assert own.status_code == 200 and graph_b["canary"] in own.text
    theirs = b_client.get(f"/api/workspaces/{graph_a['ids']['workspace_id']}")
    assert theirs.status_code == 404 and graph_a["canary"] not in theirs.text
