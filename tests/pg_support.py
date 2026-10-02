"""PostgreSQL helpers for the dual-dialect tests (Bundle 7 spec §25.1)."""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

PG_DSN = os.environ.get("JOBSEARCH_TEST_PG_DSN", "postgresql://postgres@localhost:5432/postgres")


def _pg_unavailable() -> str | None:
    try:
        import psycopg

        with psycopg.connect(PG_DSN, connect_timeout=3):
            return None
    except Exception as exc:  # pragma: no cover - environment dependent
        return f"postgres unavailable: {exc}"


PG_SKIP = _pg_unavailable()


def database_url(name: str) -> str:
    parts = urlsplit(PG_DSN)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def create_database(name: str, *, template: str | None = None) -> str:
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        clause = f" TEMPLATE {template}" if template else ""
        admin.execute(f'CREATE DATABASE "{name}"{clause}')
    return database_url(name)


def drop_database(name: str) -> None:
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@contextmanager
def fresh_pg_database(prefix: str = "jobsearch_t"):
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    url = create_database(name)
    try:
        yield url
    finally:
        drop_database(name)
