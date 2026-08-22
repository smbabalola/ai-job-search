from __future__ import annotations

import pytest

from webapp.persistence.db import connect, init_db
from webapp.persistence.search_workspaces import (
    DEFAULT_SEARCH_WORKSPACE_ID,
    SearchWorkspaceConflictError,
    SearchWorkspaceError,
    archive_search_workspace,
    create_search_workspace,
    get_search_workspace,
    rename_search_workspace,
    restore_search_workspace,
)
from webapp.persistence.user_profile import get_current_user_profile, save_user_profile


def _conn(tmp_path):
    path = tmp_path / "search-workspaces.sqlite3"
    init_db(path)
    return connect(path)


def test_create_rename_archive_and_restore_use_optimistic_revision(tmp_path):
    conn = _conn(tmp_path)
    created = create_search_workspace(conn, name="Project Manager")

    renamed = rename_search_workspace(
        conn, created["id"], name="Programme Manager", expected_revision=1
    )
    assert renamed["name"] == "Programme Manager"
    assert renamed["revision"] == 2

    with pytest.raises(SearchWorkspaceConflictError):
        rename_search_workspace(
            conn, created["id"], name="Stale edit", expected_revision=1
        )

    archived = archive_search_workspace(
        conn, created["id"], expected_revision=2
    )
    assert archived["status"] == "archived"
    restored = restore_search_workspace(
        conn, created["id"], expected_revision=3
    )
    assert restored["status"] == "active"
    assert restored["revision"] == 4


def test_last_active_search_workspace_cannot_be_archived(tmp_path):
    conn = _conn(tmp_path)

    with pytest.raises(SearchWorkspaceError, match="last active"):
        archive_search_workspace(
            conn, DEFAULT_SEARCH_WORKSPACE_ID, expected_revision=1
        )


def test_preferences_are_isolated_and_copy_is_explicit(tmp_path):
    conn = _conn(tmp_path)
    default_profile = save_user_profile(
        conn,
        {"target_roles": ["Planner"]},
        search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID,
        expected_revision=None,
    )
    blank = create_search_workspace(conn, name="Blank")
    copied = create_search_workspace(
        conn,
        name="Copied",
        copy_profile_from=DEFAULT_SEARCH_WORKSPACE_ID,
    )

    assert get_current_user_profile(conn, blank["id"]) is None
    assert get_current_user_profile(conn, copied["id"])["id"] == default_profile["id"]

    copied_profile = get_current_user_profile(conn, copied["id"])
    updated = save_user_profile(
        conn,
        {"target_roles": ["Project Manager"]},
        search_workspace_id=copied["id"],
        expected_revision=copied_profile["profile_revision"],
    )
    assert updated["payload"]["target_roles"] == ["Project Manager"]
    assert get_current_user_profile(
        conn, DEFAULT_SEARCH_WORKSPACE_ID
    )["payload"]["target_roles"] == ["Planner"]
    assert conn.execute(
        "SELECT COUNT(*) FROM search_workspace_user_profile_history "
        "WHERE search_workspace_id = ?",
        (copied["id"],),
    ).fetchone()[0] == 2


def test_stale_preference_edit_is_rejected(tmp_path):
    conn = _conn(tmp_path)
    first = save_user_profile(
        conn,
        {"target_roles": ["Planner"]},
        search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID,
        expected_revision=None,
    )
    save_user_profile(
        conn,
        {"target_roles": ["Scheduler"]},
        search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID,
        expected_revision=first["profile_revision"],
    )

    with pytest.raises(SearchWorkspaceConflictError):
        save_user_profile(
            conn,
            {"target_roles": ["Manager"]},
            search_workspace_id=DEFAULT_SEARCH_WORKSPACE_ID,
            expected_revision=first["profile_revision"],
        )
