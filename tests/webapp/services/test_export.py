"""Bundle 7 spec §20.5 (Task 28): the data export ZIP, its limits and its download."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from product.retention_policy import load_retention_policy
from webapp.app import create_app
from webapp.persistence.db import connect
from webapp.services.export import DAILY_LIMIT, SECRET_COLUMN, ExportRefused, build_export, request_export
from webapp.services.ownership import AccountScope
from webapp.storage.object_store import object_store_from_settings
from tests.webapp.admin_helpers import admin_settings
from tests.webapp.auth_helpers import csrf_token, publish_legal_documents, sign_in, sign_up_and_verify
from tests.webapp.factories import build_account_graph

POLICY = load_retention_policy("product/policies/retention-policy.dev.json")


@pytest.fixture
def world(tmp_path):
    settings = admin_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app):
        publish_legal_documents(settings)
        client = TestClient(app)
        sign_up_and_verify(client)
        sign_in(client)
        account_id = client.get("/auth/me").json()["account_id"]
        conn = connect(settings)
        graph = build_account_graph(conn, account_id=account_id, documents_root=settings.documents_root)
        user_id = conn.execute("SELECT user_id FROM account_memberships WHERE account_id = ?",
                               (account_id,)).fetchone()[0]
        scope = AccountScope(account_id=account_id, profile_root=settings.profile_root, user_id=user_id)
        yield SimpleNamespace(app=app, settings=settings, client=client, conn=conn, scope=scope, graph=graph,
                              account_id=account_id)
        conn.close()


def _export(world, now=None):
    now = now or datetime.now(timezone.utc)
    export_id = request_export(world.conn, world.scope, now=now, settings=world.settings)
    build_export(world.conn, export_id=export_id, object_store=object_store_from_settings(world.settings), now=now,
                 settings=world.settings, policy=POLICY)
    return export_id


def test_the_zip_holds_tables_documents_and_profile_without_secrets(world):
    other = TestClient(world.app)
    sign_up_and_verify(other, email="grace@example.com", name="Grace Hopper")
    sign_in(other, email="grace@example.com")
    other_graph = build_account_graph(world.conn, account_id=other.get("/auth/me").json()["account_id"],
                                      documents_root=world.settings.documents_root)
    export_id = _export(world)
    response = world.client.get(f"/settings/account/export/{export_id}/download")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["tables"]["workspaces"] >= 1 and "tables/artifacts.json" in names
    for name in (n for n in names if n.startswith("tables/")):
        for row in json.loads(archive.read(name)):
            assert not [column for column in row if SECRET_COLUMN.search(column)], name
    for name in names:  # nothing of another account
        assert other_graph["canary"].encode() not in archive.read(name), name
        assert b"grace@example.com" not in archive.read(name), name
    assert world.graph["canary"].encode() in archive.read("tables/workspaces.json")
    assert "tables/web_sessions.json" not in names and "tables/user_identities.json" not in names  # internal tables
    document = world.conn.execute("SELECT id, sha256 FROM application_document_versions WHERE account_id = ?",
                                  (world.account_id,)).fetchone()
    stored = [n for n in names if n.startswith(f"documents/{document[0]}-")]
    assert len(stored) == 1 and hashlib.sha256(archive.read(stored[0])).hexdigest() == document[1]
    profile = [n for n in names if n.startswith("profile/")]
    assert profile and "Ada" in archive.read(profile[0]).decode()
    actions = [r[0] for r in world.conn.execute("SELECT action FROM audit_log WHERE account_id = ?", (world.account_id,))]
    assert "DATA_EXPORT_REQUESTED" in actions and "DATA_EXPORT_DOWNLOADED" in actions
    kinds = [r[0] for r in world.conn.execute("SELECT kind FROM notifications WHERE account_id = ?", (world.account_id,))]
    assert "account.data_export_ready" in kinds


def test_downloading_needs_the_accounts_session(world):
    export_id = _export(world)
    anonymous = TestClient(world.app)
    assert anonymous.get(f"/settings/account/export/{export_id}/download", follow_redirects=False).status_code in (303, 401)
    other = TestClient(world.app)
    sign_up_and_verify(other, email="grace@example.com", name="Grace Hopper")
    sign_in(other, email="grace@example.com")
    assert other.get(f"/settings/account/export/{export_id}/download").status_code == 404


def test_one_at_a_time_and_three_a_day(world):
    now = datetime.now(timezone.utc)
    request_export(world.conn, world.scope, now=now, settings=world.settings)
    with pytest.raises(ExportRefused, match="already"):
        request_export(world.conn, world.scope, now=now, settings=world.settings)
    world.conn.execute("UPDATE account_exports SET status = 'READY'")
    world.conn.commit()
    for _ in range(DAILY_LIMIT - 1):
        export_id = request_export(world.conn, world.scope, now=now, settings=world.settings)
        world.conn.execute("UPDATE account_exports SET status = 'READY' WHERE id = ?", (export_id,))
        world.conn.commit()
    with pytest.raises(ExportRefused) as refused:
        request_export(world.conn, world.scope, now=now, settings=world.settings)
    assert refused.value.code == "RATE_LIMITED"
    request_export(world.conn, world.scope, now=now + timedelta(days=1, minutes=1), settings=world.settings)


def test_an_expired_link_is_refused(world):
    export_id = _export(world, now=datetime.now(timezone.utc) - timedelta(days=8))
    assert world.client.get(f"/settings/account/export/{export_id}/download").status_code == 410


def test_the_export_page_requests_an_export(world):
    response = world.client.post("/settings/account/export", data={"csrf_token": csrf_token(world.client)},
                                 follow_redirects=False)
    assert response.status_code == 303
    assert "Preparing" in world.client.get("/settings/account/export").text


def test_an_export_whose_job_died_does_not_block_the_next_one(world):
    """A build that kept failing leaves its job DEAD; the export is then FAILED and
    the person can ask again (export is a never-gated privacy surface)."""
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    first = request_export(world.conn, world.scope, now=now, settings=world.settings)
    world.conn.execute("UPDATE jobs SET status = 'DEAD' WHERE dedupe_key = ?", (f"export:{first}",))
    world.conn.commit()
    second = request_export(world.conn, world.scope, now=now + timedelta(minutes=5), settings=world.settings)
    statuses = dict(world.conn.execute("SELECT id, status FROM account_exports WHERE id IN (?, ?)",
                                       (first, second)).fetchall())
    assert statuses == {first: "FAILED", second: "QUEUED"}
