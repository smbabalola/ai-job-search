"""Bundle 7 Task 4: profile source store and per-account user-profile versions (spec H6, H7, §10.4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from product.profile_snapshot import SOURCE_PATHS, OverlaySourceReader, build_snapshot
from webapp.persistence import dbapi
from webapp.persistence.accounts import DEFAULT_ACCOUNT_ID, create_account
from webapp.persistence.db import connect, init_db
from webapp.persistence.profile_sources import CANDIDATE_SOURCE
from webapp.storage.profile_sources import (
    DatabaseProfileSourceStore,
    FilesystemProfileSourceStore,
    ProfileSourceConflict,
    as_profile_sources,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "webapp_profile_root"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "db.sqlite3"
    init_db(path)
    connection = connect(path)
    yield connection
    connection.close()


def _load_fixture(sources) -> None:
    for relative in SOURCE_PATHS:
        path = FIXTURE_ROOT / relative
        if path.is_file():
            sources.write(relative, path.read_text(encoding="utf-8"))


def test_filesystem_sources_read_write_and_digest(tmp_path):
    sources = FilesystemProfileSourceStore(tmp_path).for_account(None, DEFAULT_ACCOUNT_ID)
    assert sources.read(CANDIDATE_SOURCE) is None and sources.digest(CANDIDATE_SOURCE) is None
    sources.write(CANDIDATE_SOURCE, "# Candidate Profile\n")
    assert (tmp_path / CANDIDATE_SOURCE).read_text(encoding="utf-8") == "# Candidate Profile\n"
    assert sources.read(CANDIDATE_SOURCE) == "# Candidate Profile\n"
    assert len(sources.digest(CANDIDATE_SOURCE)) == 64
    sources.delete(CANDIDATE_SOURCE)
    assert sources.read(CANDIDATE_SOURCE) is None


def test_filesystem_store_uses_per_account_roots(tmp_path):
    store = FilesystemProfileSourceStore(tmp_path)
    assert store.for_account(None, DEFAULT_ACCOUNT_ID).root == tmp_path.resolve()
    assert store.for_account(None, "acct_x").root == tmp_path.resolve() / ".jobsearch" / "accounts" / "acct_x"


def test_database_sources_are_revisioned_and_optimistic(conn):
    sources = DatabaseProfileSourceStore().for_account(conn, DEFAULT_ACCOUNT_ID)
    assert sources.revision(CANDIDATE_SOURCE) == 0 and sources.read(CANDIDATE_SOURCE) is None
    assert sources.write(CANDIDATE_SOURCE, "v1", expected_revision=0) == 1
    assert sources.write(CANDIDATE_SOURCE, "v2") == 2
    conn.commit()
    with pytest.raises(ProfileSourceConflict):
        sources.write(CANDIDATE_SOURCE, "v3", expected_revision=1)
    assert sources.read(CANDIDATE_SOURCE) == "v2"
    sources.delete(CANDIDATE_SOURCE)
    assert sources.read(CANDIDATE_SOURCE) is None and sources.revision(CANDIDATE_SOURCE) == 3
    conn.commit()


def test_database_sources_are_isolated_per_account(conn):
    create_account(conn, display_name="Other", account_id="acct_other")
    store = DatabaseProfileSourceStore()
    store.for_account(conn, DEFAULT_ACCOUNT_ID).write(CANDIDATE_SOURCE, "mine")
    conn.commit()
    assert store.for_account(conn, "acct_other").read(CANDIDATE_SOURCE) is None


def test_database_source_history_is_append_only(conn):
    DatabaseProfileSourceStore().for_account(conn, DEFAULT_ACCOUNT_ID).write(CANDIDATE_SOURCE, "v1")
    conn.commit()
    with pytest.raises(dbapi.IntegrityError, match="append-only"):
        conn.execute("UPDATE profile_source_revisions SET content = 'x'")
    conn.rollback()
    with pytest.raises(dbapi.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM profile_source_revisions")
    conn.rollback()


def test_database_reader_builds_the_same_snapshot_as_the_filesystem(conn):
    sources = DatabaseProfileSourceStore().for_account(conn, DEFAULT_ACCOUNT_ID)
    _load_fixture(sources)
    conn.commit()
    included = (CANDIDATE_SOURCE,)
    assert build_snapshot(sources.reader(), included_sources=included) == build_snapshot(
        FIXTURE_ROOT, included_sources=included)


def test_overlay_reader_replaces_one_source_only(conn):
    sources = FilesystemProfileSourceStore(FIXTURE_ROOT).for_account(None, DEFAULT_ACCOUNT_ID)
    overlay = OverlaySourceReader(sources.reader(), {CANDIDATE_SOURCE: "# replaced"})
    assert overlay.read(CANDIDATE_SOURCE) == "# replaced"
    assert overlay.read("CLAUDE.md") == (FIXTURE_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_as_profile_sources_accepts_a_path_or_a_bound_handle(tmp_path, conn):
    from_path = as_profile_sources(tmp_path)
    assert from_path.root == tmp_path.resolve()
    handle = DatabaseProfileSourceStore().for_account(conn, DEFAULT_ACCOUNT_ID)
    assert as_profile_sources(handle) is handle


def test_user_profile_versions_are_never_shared_across_accounts(conn):
    from webapp.persistence.search_workspaces import create_search_workspace
    from webapp.persistence.user_profile import get_current_user_profile, save_user_profile

    create_account(conn, display_name="Other", account_id="acct_other")
    other_ws = create_search_workspace(conn, name="Other search", account_id="acct_other")
    profile = {"schema_version": "user-profile.v2", "target_roles": ["Drilling Engineer"]}
    mine = save_user_profile(conn, profile, search_workspace_id="search_default", account_id=DEFAULT_ACCOUNT_ID)
    theirs = save_user_profile(conn, profile, search_workspace_id=other_ws["id"], account_id="acct_other")
    assert mine["content_id"] == theirs["content_id"]
    assert mine["id"] != theirs["id"]
    rows = conn.execute("SELECT account_id FROM user_profile_versions ORDER BY account_id").fetchall()
    assert [r["account_id"] for r in rows] == ["account_local", "acct_other"]
    assert get_current_user_profile(conn, other_ws["id"], account_id="acct_other")["id"] == theirs["id"]
