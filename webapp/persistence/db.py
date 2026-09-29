from __future__ import annotations

from pathlib import Path

from webapp.persistence import dbapi

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(target) -> "dbapi.Connection":
    """target: Settings, a postgresql:// URL, or a SQLite path (spec §10.1)."""
    return dbapi.connect(target)


def init_db(db_path: Path) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        from webapp.persistence.migrations import apply_migrations

        apply_migrations(conn)
    finally:
        conn.close()
