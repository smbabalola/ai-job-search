from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


DEFAULT_SEARCH_WORKSPACE_ID = "search_default"


class SearchWorkspaceError(ValueError):
    pass


class SearchWorkspaceConflictError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_search_workspace(
    conn: sqlite3.Connection, search_workspace_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM search_workspaces WHERE id = ?", (search_workspace_id,)
    ).fetchone()
    return dict(row) if row else None


def list_search_workspaces(
    conn: sqlite3.Connection, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    if include_archived:
        rows = conn.execute(
            "SELECT * FROM search_workspaces ORDER BY status, updated_at DESC, id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM search_workspaces WHERE status = 'active' "
            "ORDER BY updated_at DESC, id"
        ).fetchall()
    return [dict(row) for row in rows]


def create_search_workspace(
    conn: sqlite3.Connection,
    *,
    name: str,
    search_workspace_id: str | None = None,
    copy_profile_from: str | None = None,
) -> dict[str, Any]:
    normalized_name = " ".join(name.split())
    if not normalized_name:
        raise SearchWorkspaceError("search workspace name is required")
    workspace_id = search_workspace_id or f"search_{uuid.uuid4().hex[:20]}"
    now = _now()
    conn.execute(
        "INSERT INTO search_workspaces "
        "(id, name, status, revision, created_at, updated_at, archived_at) "
        "VALUES (?, ?, 'active', 1, ?, ?, NULL)",
        (workspace_id, normalized_name, now, now),
    )
    if copy_profile_from is not None:
        source = conn.execute(
            "SELECT current_version_id FROM search_workspace_user_profiles "
            "WHERE search_workspace_id = ?",
            (copy_profile_from,),
        ).fetchone()
        if source is None:
            conn.rollback()
            raise SearchWorkspaceError(
                f"search workspace {copy_profile_from!r} has no preferences to copy"
            )
        conn.execute(
            "INSERT INTO search_workspace_user_profiles "
            "(search_workspace_id, current_version_id, revision, updated_at) "
            "VALUES (?, ?, 1, ?)",
            (workspace_id, source["current_version_id"], now),
        )
        conn.execute(
            "INSERT INTO search_workspace_user_profile_history "
            "(id, search_workspace_id, version_id, previous_version_id, assigned_at) "
            "VALUES (?, ?, ?, NULL, ?)",
            (
                f"swuph_{uuid.uuid4().hex[:20]}",
                workspace_id,
                source["current_version_id"],
                now,
            ),
        )
    conn.commit()
    return get_search_workspace(conn, workspace_id)


def _mutate_workspace(
    conn: sqlite3.Connection,
    search_workspace_id: str,
    *,
    expected_revision: int,
    updates: dict[str, Any],
) -> dict[str, Any]:
    workspace = get_search_workspace(conn, search_workspace_id)
    if workspace is None:
        raise SearchWorkspaceError(f"unknown search workspace {search_workspace_id!r}")
    if workspace["revision"] != expected_revision:
        raise SearchWorkspaceConflictError(
            "search workspace changed after this page was loaded"
        )
    assignments = [f"{field} = ?" for field in updates]
    values = list(updates.values())
    assignments.extend(["revision = revision + 1", "updated_at = ?"])
    values.extend([_now(), search_workspace_id, expected_revision])
    cursor = conn.execute(
        f"UPDATE search_workspaces SET {', '.join(assignments)} "
        "WHERE id = ? AND revision = ?",
        values,
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise SearchWorkspaceConflictError(
            "search workspace changed after this page was loaded"
        )
    conn.commit()
    return get_search_workspace(conn, search_workspace_id)


def rename_search_workspace(
    conn: sqlite3.Connection,
    search_workspace_id: str,
    *,
    name: str,
    expected_revision: int,
) -> dict[str, Any]:
    normalized_name = " ".join(name.split())
    if not normalized_name:
        raise SearchWorkspaceError("search workspace name is required")
    return _mutate_workspace(
        conn,
        search_workspace_id,
        expected_revision=expected_revision,
        updates={"name": normalized_name},
    )


def archive_search_workspace(
    conn: sqlite3.Connection,
    search_workspace_id: str,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    workspace = get_search_workspace(conn, search_workspace_id)
    if workspace is None:
        raise SearchWorkspaceError(f"unknown search workspace {search_workspace_id!r}")
    if workspace["revision"] != expected_revision:
        raise SearchWorkspaceConflictError(
            "search workspace changed after this page was loaded"
        )
    if workspace["status"] == "archived":
        return workspace
    active_count = conn.execute(
        "SELECT COUNT(*) FROM search_workspaces WHERE status = 'active'"
    ).fetchone()[0]
    if active_count <= 1:
        raise SearchWorkspaceError("the last active search workspace cannot be archived")
    return _mutate_workspace(
        conn,
        search_workspace_id,
        expected_revision=expected_revision,
        updates={"status": "archived", "archived_at": _now()},
    )


def restore_search_workspace(
    conn: sqlite3.Connection,
    search_workspace_id: str,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    workspace = get_search_workspace(conn, search_workspace_id)
    if workspace is None:
        raise SearchWorkspaceError(f"unknown search workspace {search_workspace_id!r}")
    if workspace["revision"] != expected_revision:
        raise SearchWorkspaceConflictError(
            "search workspace changed after this page was loaded"
        )
    if workspace["status"] == "active":
        return workspace
    return _mutate_workspace(
        conn,
        search_workspace_id,
        expected_revision=expected_revision,
        updates={"status": "active", "archived_at": None},
    )
