from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from webapp.persistence import dbapi

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
PG_BASELINE_PATH = Path(__file__).with_name("pg") / "0001_baseline.sql"
PG_INIT_LOCK_KEY = 0x4A53_0002


def connect(target) -> "dbapi.Connection":
    """target: Settings, a postgresql:// URL, or a SQLite path (spec §10.1)."""
    return dbapi.connect(target)


def init_db(target) -> None:
    """Create or upgrade the schema. SQLite runs schema.sql plus the migration
    chain; PostgreSQL loads the 021-equivalent baseline once, then the Bundle 7
    chain (spec H5). A hosted Settings target never seeds the local account."""
    dialect, location, settings = dbapi.resolve(target)
    if dialect == "sqlite":
        _init_sqlite(location)
    else:
        _init_postgres(location, seed_local=not (settings is not None and settings.is_hosted))


def _init_sqlite(db_path: Path) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        from webapp.persistence.migrations import apply_migrations

        apply_migrations(conn)
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_postgres(url: str, *, seed_local: bool) -> None:
    from webapp.persistence.accounts import DEFAULT_ACCOUNT_DISPLAY_NAME, DEFAULT_ACCOUNT_ID
    from webapp.persistence.migrations import LEGACY_MIGRATION_IDS, apply_migrations
    from webapp.persistence.search_workspaces import DEFAULT_SEARCH_WORKSPACE_ID

    conn = connect(url)
    try:
        # Serialize concurrent initializers (several processes starting at once).
        conn.execute("SELECT pg_advisory_lock(?)", (PG_INIT_LOCK_KEY,))
        try:
            fresh = conn.execute("SELECT to_regclass('schema_migrations') IS NULL AS fresh").fetchone()["fresh"]
            if fresh:
                conn.execute("BEGIN")
                conn.execute(PG_BASELINE_PATH.read_text(encoding="utf-8"))
                now = _now()
                conn.executemany(
                    "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                    [(migration_id, now) for migration_id in LEGACY_MIGRATION_IDS],
                )
                if seed_local:
                    conn.execute(
                        "INSERT INTO accounts (id, display_name, created_at) VALUES (?, ?, ?)",
                        (DEFAULT_ACCOUNT_ID, DEFAULT_ACCOUNT_DISPLAY_NAME, now),
                    )
                    conn.execute(
                        "INSERT INTO search_workspaces (id, name, status, revision, created_at, updated_at, "
                        "archived_at, account_id) VALUES (?, 'Default search', 'active', 1, ?, ?, NULL, ?)",
                        (DEFAULT_SEARCH_WORKSPACE_ID, now, now, DEFAULT_ACCOUNT_ID),
                    )
                conn.commit()
            apply_migrations(conn)
        finally:
            conn.execute("SELECT pg_advisory_unlock(?)", (PG_INIT_LOCK_KEY,))
    finally:
        conn.close()
