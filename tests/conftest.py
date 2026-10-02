"""Dual-dialect test harness (Bundle 7 spec §25.1).

``pytest --db postgres`` runs the same tests against PostgreSQL: every SQLite
file path a test hands to ``init_db``/``connect`` is mapped to its own
database, cloned from a migrated template built once per session, and dropped
after the test. The default (``--db sqlite``) changes nothing.
"""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--db", choices=("sqlite", "postgres"), default="sqlite",
                     help="database dialect for persistence-backed tests")


class _PgRedirect:
    def __init__(self, template: str) -> None:
        self.template = template
        self.by_path: dict[str, tuple[str, str]] = {}
        self.lock = threading.Lock()

    def __call__(self, path: Path) -> str | None:
        from tests.pg_support import create_database

        key = os.path.normcase(str(Path(path).resolve()))
        with self.lock:
            if key not in self.by_path:
                name = f"jobsearch_t_{uuid.uuid4().hex[:12]}"
                self.by_path[key] = (name, create_database(name, template=self.template))
            return self.by_path[key][1]

    def drop_all(self) -> None:
        from tests.pg_support import drop_database

        with self.lock:
            names = [name for name, _ in self.by_path.values()]
            self.by_path.clear()
        for name in names:
            drop_database(name)


@pytest.fixture(scope="session")
def db_dialect(request) -> str:
    return request.config.getoption("--db")


@pytest.fixture(scope="session")
def _pg_redirect(db_dialect):
    if db_dialect != "postgres":
        yield None
        return
    from tests.pg_support import PG_SKIP, create_database, drop_database
    from webapp.persistence import dbapi
    from webapp.persistence.db import init_db

    if PG_SKIP:
        pytest.exit(f"--db postgres requested but {PG_SKIP}", returncode=2)
    template = f"jobsearch_template_{os.getpid()}"
    drop_database(template)
    init_db(create_database(template))
    redirect = _PgRedirect(template)
    dbapi.set_sqlite_redirect(redirect)
    try:
        yield redirect
    finally:
        dbapi.set_sqlite_redirect(None)
        redirect.drop_all()
        drop_database(template)


@pytest.fixture(autouse=True)
def _pg_databases_per_test(_pg_redirect):
    yield
    if _pg_redirect is not None:
        _pg_redirect.drop_all()


@pytest.fixture
def db_settings(tmp_path):
    """Settings for the selected dialect (the redirect maps db_path under --db postgres)."""
    from webapp.config import Settings

    return Settings(db_path=tmp_path / "jobsearch.sqlite3")


_SQLITE_CHAIN_REASON = ("tests the SQLite legacy migration chain 001-021; PostgreSQL starts from the "
                        "parity-verified baseline (spec H5, tests/webapp/persistence/test_schema_parity.py)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--db") != "postgres":
        skip_pg = pytest.mark.skip(reason="exercises PostgreSQL-specific behaviour; run with --db postgres")
        for item in items:
            if item.get_closest_marker("postgres_only"):
                item.add_marker(skip_pg)
        return
    skip = pytest.mark.skip(reason=_SQLITE_CHAIN_REASON)
    for item in items:
        filename = Path(str(item.fspath)).name
        if ("migration" in filename or "upgrade" in filename or item.name.startswith("test_migration_")
                or item.get_closest_marker("sqlite_only")):
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: exercises SQLite-specific behaviour; skipped under --db postgres")
    config.addinivalue_line("markers", "postgres_only: exercises PostgreSQL-specific behaviour; runs only under --db postgres")
