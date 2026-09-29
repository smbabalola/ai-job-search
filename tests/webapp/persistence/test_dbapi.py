"""Bundle 7 Task 2: the dbapi connection adapter (spec H2, H3, §10.1, §10.7)."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid

import pytest

from webapp.persistence import dbapi

PG_DSN = os.environ.get("JOBSEARCH_TEST_PG_DSN", "postgresql://postgres@localhost:5432/postgres")


def _pg_available() -> str | None:
    try:
        import psycopg

        with psycopg.connect(PG_DSN, connect_timeout=3):
            return None
    except Exception as exc:  # pragma: no cover - environment dependent
        return f"postgres unavailable: {exc}"


PG_SKIP = _pg_available()


@pytest.fixture
def pg_schema():
    if PG_SKIP:
        pytest.skip(PG_SKIP)
    import psycopg

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in PG_DSN else "?"
    yield f"{PG_DSN}{sep}options=-csearch_path%3D{schema}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(params=["sqlite", "postgres"])
def target(request, tmp_path):
    if request.param == "sqlite":
        return tmp_path / "t.sqlite3"
    return request.getfixturevalue("pg_schema")


def _setup(conn):
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY, n INTEGER NOT NULL, note TEXT)")
    conn.commit()


# ---- placeholder translation (pure) -----------------------------------------

@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT '?' , ? FROM t WHERE a LIKE '%x%'", "SELECT '?' , %s FROM t WHERE a LIKE '%%x%%'"),
        ("SELECT ? -- is this ?\nFROM t", "SELECT %s -- is this ?\nFROM t"),
        ('SELECT "col?" FROM t WHERE a = ?', 'SELECT "col?" FROM t WHERE a = %s'),
        ("SELECT 'it''s ?' , ?", "SELECT 'it''s ?' , %s"),
        ("SELECT a FROM t WHERE b IS ? AND c IS NOT ?",
         "SELECT a FROM t WHERE b IS NOT DISTINCT FROM %s AND c IS DISTINCT FROM %s"),
        ("SELECT a FROM t WHERE b IS NULL", "SELECT a FROM t WHERE b IS NULL"),
    ],
)
def test_translate_placeholders(sql, expected):
    assert dbapi.translate_placeholders(sql) == expected


# ---- behaviour on both dialects ---------------------------------------------

def test_dialect_and_row_access(target):
    conn = dbapi.connect(target)
    try:
        assert conn.dialect == ("sqlite" if not isinstance(target, str) else "postgres")
        _setup(conn)
        conn.execute("INSERT INTO t (id, n, note) VALUES (?, ?, ?)", ("a", 1, "x%y"))
        conn.commit()
        row = conn.execute("SELECT id, n, note FROM t WHERE id = ?", ("a",)).fetchone()
        assert row["id"] == "a" and row[1] == 1 and row["note"] == "x%y"
        assert dict(row) == {"id": "a", "n": 1, "note": "x%y"}
        assert list(row.keys()) == ["id", "n", "note"]
        assert conn.execute("SELECT id FROM t WHERE id = ?", ("zz",)).fetchone() is None
        assert [tuple(r) for r in conn.execute("SELECT id, n FROM t")] == [("a", 1)]
        total = conn.execute("SELECT COALESCE(SUM(n), 0) AS total FROM t").fetchone()["total"]
        assert total == 1 and isinstance(total, int)
    finally:
        conn.close()


def test_duplicate_primary_key_raises_integrity_error_and_transaction_survives(target):
    conn = dbapi.connect(target)
    try:
        _setup(conn)
        conn.execute("BEGIN")
        conn.execute("INSERT INTO t (id, n) VALUES (?, ?)", ("a", 1))
        with pytest.raises(dbapi.IntegrityError) as excinfo:
            conn.execute("INSERT INTO t (id, n) VALUES (?, ?)", ("a", 2))
        assert isinstance(excinfo.value, sqlite3.IntegrityError)
        # SQLite keeps the transaction usable after a failed statement; so must PostgreSQL.
        conn.execute("INSERT INTO t (id, n) VALUES (?, ?)", ("b", 3))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"] == 2
    finally:
        conn.close()


def test_in_transaction_tracks_implicit_and_explicit_transactions(target):
    conn = dbapi.connect(target)
    try:
        _setup(conn)
        assert conn.in_transaction is False
        conn.execute("SELECT 1")
        assert conn.in_transaction is False
        conn.execute("INSERT INTO t (id, n) VALUES (?, ?)", ("a", 1))
        assert conn.in_transaction is True
        conn.commit()
        assert conn.in_transaction is False
        conn.execute("BEGIN IMMEDIATE")
        assert conn.in_transaction is True
        conn.rollback()
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_returning_and_rowcount(target):
    conn = dbapi.connect(target)
    try:
        _setup(conn)
        row = conn.execute("INSERT INTO t (id, n) VALUES (?, ?) RETURNING id, n", ("a", 7)).fetchone()
        assert (row["id"], row["n"]) == ("a", 7)
        cursor = conn.execute("UPDATE t SET n = n + 1 WHERE id = ?", ("a",))
        assert cursor.rowcount == 1
        conn.commit()
    finally:
        conn.close()


def test_on_conflict_do_nothing(target):
    conn = dbapi.connect(target)
    try:
        _setup(conn)
        for _ in range(2):
            conn.execute("INSERT INTO t (id, n) VALUES (?, ?) ON CONFLICT (id) DO NOTHING", ("a", 1))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"] == 1
    finally:
        conn.close()


def test_begin_immediate_records_lock_stats_per_site(target):
    dbapi.LOCK_STATS.reset()
    conn = dbapi.connect(target)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.commit()
    finally:
        conn.close()
    site = f"{__name__}:test_begin_immediate_records_lock_stats_per_site"
    stats = dbapi.LOCK_STATS.snapshot()
    assert stats[site]["acquisitions"] == 1
    assert stats[site]["timeouts"] == 0
    assert stats[site]["hold_seconds_total"] >= 0


# ---- PostgreSQL-only concurrency -------------------------------------------

def test_begin_immediate_serializes_writers_on_postgres(pg_schema):
    first = dbapi.connect(pg_schema)
    second = dbapi.connect(pg_schema)
    try:
        _setup(first)
        first.execute("BEGIN IMMEDIATE")
        first.execute("INSERT INTO t (id, n) VALUES (?, ?)", ("a", 1))
        acquired = threading.Event()
        seen: list[int] = []

        def contender():
            second.execute("BEGIN IMMEDIATE")
            acquired.set()
            seen.append(second.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"])
            second.commit()

        thread = threading.Thread(target=contender)
        thread.start()
        assert not acquired.wait(0.5), "second writer must wait for the first"
        first.commit()
        assert acquired.wait(2.0)
        thread.join(2.0)
        assert seen == [1], "the second writer sees the first writer's commit"
    finally:
        first.close()
        second.close()


def test_writer_lock_acquisition_is_bounded_on_postgres(pg_schema, caplog):
    holder = dbapi.connect(pg_schema, writer_lock_timeout_ms=1000)
    waiter = dbapi.connect(pg_schema, writer_lock_timeout_ms=1000)
    try:
        _setup(holder)
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(dbapi.DatabaseBusy) as excinfo:
            waiter.execute("BEGIN IMMEDIATE")
        elapsed = time.monotonic() - started
        assert excinfo.value.reason == "writer_lock_timeout"
        assert excinfo.value.site.endswith(":test_writer_lock_acquisition_is_bounded_on_postgres")
        assert 0.9 <= elapsed <= 3.0
        assert waiter.in_transaction is False
        waiter.execute("INSERT INTO t (id, n) VALUES (?, ?)", ("w", 1))  # usable afterwards
        waiter.rollback()
        holder.rollback()
        assert holder.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"] == 0
    finally:
        holder.close()
        waiter.close()


def test_slow_writer_lock_wait_is_logged_on_postgres(pg_schema, caplog):
    first = dbapi.connect(pg_schema)
    second = dbapi.connect(pg_schema)
    try:
        first.execute("BEGIN IMMEDIATE")

        def release_later():
            time.sleep(1.2)
            first.commit()

        thread = threading.Thread(target=release_later)
        thread.start()
        with caplog.at_level(logging.WARNING, logger="webapp.persistence.dbapi"):
            second.execute("BEGIN IMMEDIATE")
        second.commit()
        thread.join(2.0)
        assert any("writer_lock_slow" in record.getMessage() for record in caplog.records)
    finally:
        first.close()
        second.close()


def test_conflicting_repeatable_read_updates_raise_database_busy_on_postgres(pg_schema):
    a = dbapi.connect(pg_schema)
    b = dbapi.connect(pg_schema)
    try:
        _setup(a)
        a.execute("INSERT INTO t (id, n) VALUES (?, ?)", ("x", 0))
        a.commit()
        a.execute("BEGIN")
        b.execute("BEGIN")
        a.execute("SELECT n FROM t WHERE id = ?", ("x",)).fetchone()
        b.execute("SELECT n FROM t WHERE id = ?", ("x",)).fetchone()
        a.execute("UPDATE t SET n = n + 1 WHERE id = ?", ("x",))
        a.commit()
        with pytest.raises(dbapi.DatabaseBusy) as excinfo:
            b.execute("UPDATE t SET n = n + 1 WHERE id = ?", ("x",))
        assert excinfo.value.reason == "serialization"
        b.rollback()
    finally:
        a.close()
        b.close()


def test_pragmas_on_postgres(pg_schema):
    conn = dbapi.connect(pg_schema)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        with pytest.raises(dbapi.OperationalError, match="pragma not supported on postgres"):
            conn.execute("PRAGMA foreign_key_check")
    finally:
        conn.close()
