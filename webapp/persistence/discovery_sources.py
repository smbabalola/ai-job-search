from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_discovery_source_settings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All registry rows, regardless of enabled state -- ordered by source_id
    for deterministic output. Never filtered against code-level adapters:
    callers that need runtime-available sources should use
    product.discovery_search.available_discovery_source_ids instead."""
    return [
        {
            "source_id": row["source_id"],
            "display_name": row["display_name"],
            "enabled": bool(row["enabled"]),
            "updated_at": row["updated_at"],
        }
        for row in conn.execute(
            "SELECT source_id, display_name, enabled, updated_at "
            "FROM discovery_source_settings ORDER BY source_id"
        )
    ]


def list_enabled_discovery_source_ids(conn: sqlite3.Connection) -> list[str]:
    """Source IDs with enabled = 1 in the registry -- not yet intersected
    with code-level adapter availability. Callers deciding what is actually
    runnable must still intersect against SOURCE_CLI_PATHS.keys()."""
    return [
        row["source_id"]
        for row in conn.execute(
            "SELECT source_id FROM discovery_source_settings "
            "WHERE enabled = 1 ORDER BY source_id"
        )
    ]


def set_discovery_source_enabled(
    conn: sqlite3.Connection, source_id: str, enabled: bool, *, commit: bool = True
) -> dict[str, Any]:
    """Toggle a registry row's enabled state. Raises KeyError for a
    source_id with no registry row -- this function never creates a new
    row, since a row with no matching code-level adapter would be inert and
    misleading (see the registry's design note in migrations.py)."""
    cursor = conn.execute(
        "UPDATE discovery_source_settings SET enabled = ?, updated_at = ? "
        "WHERE source_id = ?",
        (1 if enabled else 0, _now(), source_id),
    )
    if cursor.rowcount == 0:
        raise KeyError(f"unknown discovery source {source_id!r}")
    if commit:
        conn.commit()
    row = conn.execute(
        "SELECT source_id, display_name, enabled, updated_at "
        "FROM discovery_source_settings WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    return {
        "source_id": row["source_id"],
        "display_name": row["display_name"],
        "enabled": bool(row["enabled"]),
        "updated_at": row["updated_at"],
    }
