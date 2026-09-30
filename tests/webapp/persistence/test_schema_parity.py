"""Bundle 7 spec H5, §10.3: the PostgreSQL schema equals the SQLite chain."""
from __future__ import annotations

import json

import pytest

from tests.pg_support import PG_SKIP, fresh_pg_database
from webapp.persistence import dbapi
from webapp.persistence.db import connect, init_db
from webapp.persistence.schema_catalog import schema_catalog


@pytest.fixture
def pg_url():
    if PG_SKIP:
        pytest.skip(PG_SKIP)
    with fresh_pg_database() as url:
        yield url


def _diff(a: dict, b: dict) -> str:
    lines = []
    for table in sorted(set(a) | set(b)):
        if a.get(table) != b.get(table):
            lines.append(f"== {table}\n  sqlite:   {json.dumps(a.get(table), sort_keys=True)}\n"
                         f"  postgres: {json.dumps(b.get(table), sort_keys=True)}")
    return "\n".join(lines)


def test_postgres_schema_equals_the_sqlite_chain(tmp_path, pg_url):
    sqlite_path = tmp_path / "parity.sqlite3"
    init_db(sqlite_path)
    init_db(pg_url)
    sqlite_conn, pg_conn = connect(sqlite_path), connect(pg_url)
    try:
        left, right = schema_catalog(sqlite_conn), schema_catalog(pg_conn)
    finally:
        sqlite_conn.close()
        pg_conn.close()
    assert left == right, "schema drift between dialects:\n" + _diff(left, right)


def test_postgres_init_is_idempotent_and_records_the_legacy_chain(pg_url):
    init_db(pg_url)
    init_db(pg_url)
    conn = connect(pg_url)
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM schema_migrations ORDER BY id")]
        assert ids[0] == "001_search_workspaces" and "021_human_submit" in ids
        assert conn.execute("SELECT COUNT(*) AS n FROM accounts WHERE id = 'account_local'").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM discovery_source_settings").fetchone()["n"] == 4
    finally:
        conn.close()


def test_hosted_settings_init_skips_the_local_seed():
    if PG_SKIP:
        pytest.skip(PG_SKIP)
    from webapp.config import Settings

    with fresh_pg_database() as url:
        init_db(Settings(deployment="hosted", database_url=url))
        conn = connect(url)
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"] == 0
            assert conn.execute("SELECT COUNT(*) AS n FROM search_workspaces").fetchone()["n"] == 0
            assert conn.execute("SELECT COUNT(*) AS n FROM discovery_source_settings").fetchone()["n"] == 4
        finally:
            conn.close()


def test_append_only_tables_assign_seq_and_refuse_update_and_delete(pg_url):
    init_db(pg_url)
    conn = connect(pg_url)
    try:
        for i in range(2):
            conn.execute(
                "INSERT INTO autonomy_kill_switch (id, account_id, engaged, reason, actor, created_at) "
                "VALUES (?, 'account_local', 1, 'test', 'user', '2026-09-29T00:00:00+00:00')", (f"ks_{i}",))
        conn.commit()
        seqs = [r["seq"] for r in conn.execute("SELECT seq FROM autonomy_kill_switch ORDER BY seq")]
        assert len(seqs) == 2 and seqs[0] < seqs[1]
        with pytest.raises(dbapi.IntegrityError, match="append-only"):
            conn.execute("UPDATE autonomy_kill_switch SET reason = 'x' WHERE id = 'ks_0'")
        with pytest.raises(dbapi.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM autonomy_kill_switch WHERE id = 'ks_0'")
        conn.rollback()
    finally:
        conn.close()


def test_conditional_triggers_behave_like_sqlite(pg_url):
    init_db(pg_url)
    conn = connect(pg_url)
    try:
        with pytest.raises(dbapi.IntegrityError, match="search workspace account does not exist"):
            conn.execute(
                "INSERT INTO search_workspaces (id, name, status, revision, created_at, updated_at, account_id) "
                "VALUES ('sw_x', 'x', 'active', 1, 't', 't', 'account_missing')")
        with pytest.raises(dbapi.IntegrityError, match="ownership is immutable"):
            conn.execute("UPDATE search_workspaces SET account_id = 'other' WHERE id = 'search_default'")
        conn.execute("UPDATE search_workspaces SET name = 'Renamed' WHERE id = 'search_default'")  # allowed
        conn.commit()
    finally:
        conn.close()


def test_check_extraction_ignores_identifiers_that_start_with_check():
    from webapp.persistence.schema_catalog import extract_checks

    sql = "CREATE TABLE checkout_sessions (checked TEXT, status TEXT CHECK (status IN ('OPEN')), checks INT)"
    assert extract_checks(sql) == ["status IN ('OPEN')"]
