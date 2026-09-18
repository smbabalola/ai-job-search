from __future__ import annotations

import sqlite3

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.migrations import (
    DISCOVERY_SOURCE_REGISTRY_MIGRATION_ID,
    apply_migrations,
)


def _connection(tmp_path):
    db_path = tmp_path / "jobsearch.sqlite3"
    init_db(db_path)
    return connect(db_path)


def test_migration_014_creates_table_and_is_recorded(tmp_path):
    conn = _connection(tmp_path)
    applied = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE id = ?",
        (DISCOVERY_SOURCE_REGISTRY_MIGRATION_ID,),
    ).fetchone()
    assert applied is not None

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "discovery_source_settings" in tables
    conn.close()


def test_migration_014_is_idempotent(tmp_path):
    conn = _connection(tmp_path)
    apply_migrations(conn)  # second call must not raise or duplicate
    count = conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE id = ?",
        (DISCOVERY_SOURCE_REGISTRY_MIGRATION_ID,),
    ).fetchone()[0]
    assert count == 1
    # Row count reflects every source registered up to and including the
    # latest migration that inserts into this table (currently 015 adds a
    # 4th row for airswift-search) -- not migration 014's own seed count in
    # isolation, since apply_migrations always runs the full chain.
    row_count = conn.execute(
        "SELECT COUNT(*) FROM discovery_source_settings"
    ).fetchone()[0]
    assert row_count == 4
    conn.close()


def test_all_current_sources_are_enabled_after_migration(tmp_path):
    conn = _connection(tmp_path)
    rows = {
        row["source_id"]: (row["display_name"], row["enabled"])
        for row in conn.execute(
            "SELECT source_id, display_name, enabled FROM discovery_source_settings"
        )
    }
    assert rows == {
        "freehire-search": ("Freehire", 1),
        "linkedin-search": ("LinkedIn", 1),
        "energy-jobline-search": ("Energy Jobline", 1),
        "airswift-search": ("Airswift", 1),
    }
    conn.close()


def test_enabled_check_constraint_rejects_values_outside_0_or_1(tmp_path):
    conn = _connection(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO discovery_source_settings "
            "(source_id, display_name, enabled, updated_at) "
            "VALUES ('rigzone-search', 'Rigzone', 2, 'now')"
        )
    conn.close()
