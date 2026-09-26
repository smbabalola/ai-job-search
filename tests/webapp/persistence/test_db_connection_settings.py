from __future__ import annotations

import threading

from webapp.persistence.db import connect, init_db


def test_connections_use_wal_and_a_long_busy_timeout(tmp_path):
    db = tmp_path / "x.sqlite3"
    init_db(db)
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_reader_is_not_blocked_by_an_open_writer(tmp_path):
    db = tmp_path / "x.sqlite3"
    init_db(db)
    writer, reader = connect(db), connect(db)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO accounts (id, display_name, created_at) VALUES ('a2', 'x', '2026-01-01')")
        result = {}
        t = threading.Thread(target=lambda: result.setdefault(
            "n", reader.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]))
        t.start()
        t.join(timeout=5)
        assert "n" in result  # WAL: readers see the last committed state while a writer is open
        writer.rollback()
    finally:
        writer.close()
        reader.close()
