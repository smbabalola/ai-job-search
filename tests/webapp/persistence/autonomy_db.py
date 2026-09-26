"""Shared fixtures for autonomy persistence/service tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID
from webapp.persistence.db import connect, init_db
from webapp.persistence.workspaces import create_workspace

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
ACCOUNT = DEFAULT_ACCOUNT_ID


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "autonomy.sqlite3"
    init_db(db)
    c = connect(db)
    yield c
    c.close()


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "autonomy.sqlite3"
    init_db(db)
    return db


def make_workspace(conn, company="Acme", title="Drilling Fluids Engineer", workspace_id=None):
    return create_workspace(conn, company=company, title=title, workspace_id=workspace_id)["id"]
