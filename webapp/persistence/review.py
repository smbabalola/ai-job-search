from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

DISPOSITIONS = (
    "acknowledged_and_proceed",
    "omit_from_positioning",
    "requires_upstream_change",
    "resolved_by_rerun",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


SYSTEM_AUTO_CONFIRMED = "SYSTEM_AUTO_CONFIRMED"
_PROVENANCES = ("USER", SYSTEM_AUTO_CONFIRMED)
_SYSTEM_BASIS_KEYS = {"reason", "item_content_hash", "pack_revision"}


def save_review_decision(
    conn: sqlite3.Connection, *, workspace_id: str, review_item_type: str, source_artifact_id: str,
    domain_item_id: str | None, disposition: str, note: str | None = None,
    commit: bool = True, decision_provenance: str = "USER", system_basis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition: {disposition!r}")
    if decision_provenance not in _PROVENANCES:
        raise ValueError(f"unknown decision provenance: {decision_provenance!r}")
    if decision_provenance == SYSTEM_AUTO_CONFIRMED:
        # Bundle 6C: a system decision only ever accepts a mechanically
        # verified item, and always records why (spec §8.2).
        if disposition != "acknowledged_and_proceed":
            raise ValueError("a system review decision may only acknowledge an item")
        if not isinstance(system_basis, dict) or set(system_basis) != _SYSTEM_BASIS_KEYS:
            raise ValueError(f"a system review decision needs a basis with exactly {sorted(_SYSTEM_BASIS_KEYS)}")
    elif system_basis is not None:
        raise ValueError("a user review decision has no system basis")
    decision_id = f"rev_{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO review_decisions "
        "(id, workspace_id, review_item_type, source_artifact_id, domain_item_id, disposition, note, created_at, "
        "decision_provenance, system_basis_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (decision_id, workspace_id, review_item_type, source_artifact_id, domain_item_id, disposition, note, _now(),
         decision_provenance, json.dumps(system_basis, sort_keys=True) if system_basis is not None else None),
    )
    if commit:
        conn.commit()
    return dict(conn.execute("SELECT * FROM review_decisions WHERE id = ?", (decision_id,)).fetchone())


def list_review_decisions(
    conn: sqlite3.Connection, workspace_id: str, source_artifact_id: str | None = None
) -> list[dict[str, Any]]:
    # Newest first by insertion order (rowid), never by timestamp: equal or
    # skewed created_at values must not change which decision governs.
    if source_artifact_id is None:
        rows = conn.execute(
            "SELECT * FROM review_decisions WHERE workspace_id = ? ORDER BY rowid DESC", (workspace_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM review_decisions WHERE workspace_id = ? AND source_artifact_id = ? ORDER BY rowid DESC",
            (workspace_id, source_artifact_id),
        ).fetchall()
    return [dict(row) for row in rows]
