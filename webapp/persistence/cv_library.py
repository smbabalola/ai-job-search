"""CV library rows and document-version references (Bundle 7 spec §14.1, L3)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from webapp.persistence import dbapi
from webapp.persistence.bundle7_migrations import REFERRER_TYPES, binding_documents


def ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def add_references(conn: dbapi.Connection, *, document_version_ids: Iterable[str], referrer_type: str,
                   referrer_id: str, now: datetime) -> None:
    """In the referrer's own transaction (no commit). A referenced document
    version can never be deleted (the 032 trigger)."""
    if referrer_type not in REFERRER_TYPES:
        raise ValueError(f"unknown referrer type {referrer_type}")
    for document_version_id in sorted(set(document_version_ids)):
        conn.execute("INSERT INTO document_version_references (document_version_id, referrer_type, referrer_id, "
                     "created_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                     (document_version_id, referrer_type, referrer_id, ts(now)))


def approval_documents(conn: dbapi.Connection, approval_id: str | None) -> list[str]:
    if approval_id is None:
        return []
    row = conn.execute("SELECT binding_json FROM application_approvals WHERE id = ?", (approval_id,)).fetchone()
    return [] if row is None else binding_documents(json.loads(row[0] or "{}"))


def fill_run_documents(conn: dbapi.Connection, fill_run_id: str | None) -> list[str]:
    if fill_run_id is None:
        return []
    row = conn.execute("SELECT approval_id FROM fill_run_grant_bindings WHERE fill_run_id = ?",
                       (fill_run_id,)).fetchone()
    return [] if row is None else approval_documents(conn, row[0])


# ---- items and versions ------------------------------------------------------------------

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


_VERSION_SELECT = ("SELECT v.*, d.media_type, d.original_filename, d.byte_length, d.sha256, d.storage_key "
                   "FROM cv_library_versions v JOIN application_document_versions d ON d.id = v.document_version_id "
                   "AND d.account_id = v.account_id")


def get_item(conn: dbapi.Connection, *, account_id: str, item_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM cv_library_items WHERE id = ? AND account_id = ?",
                       (item_id, account_id)).fetchone()
    return None if row is None else dict(row)


def list_items(conn: dbapi.Connection, *, account_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM cv_library_items WHERE account_id = ? "
                                          "ORDER BY status, updated_at DESC, id", (account_id,))]


def insert_item(conn: dbapi.Connection, *, account_id: str, title: str, description: str, now: datetime) -> dict:
    item_id = new_id("cvi")
    conn.execute("INSERT INTO cv_library_items (id, account_id, title, description, status, created_at, updated_at) "
                 "VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)", (item_id, account_id, title, description, ts(now), ts(now)))
    return get_item(conn, account_id=account_id, item_id=item_id)  # type: ignore[return-value]


def set_status(conn: dbapi.Connection, *, account_id: str, item_id: str, status: str, now: datetime) -> bool:
    cursor = conn.execute("UPDATE cv_library_items SET status = ?, updated_at = ? WHERE id = ? AND account_id = ?",
                          (status, ts(now), item_id, account_id))
    return cursor.rowcount == 1


def next_version_no(conn: dbapi.Connection, item_id: str) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version_no), 0) FROM cv_library_versions WHERE item_id = ?",
                       (item_id,)).fetchone()
    return int(row[0]) + 1


def insert_version(conn: dbapi.Connection, *, account_id: str, item_id: str, document_version_id: str, origin: str,
                   parent_version_id: str | None, template_id: str | None, library_visible: bool, note: str,
                   created_by: str, now: datetime) -> dict[str, Any]:
    version_id = new_id("cvv")
    conn.execute(
        "INSERT INTO cv_library_versions (id, account_id, item_id, version_no, document_version_id, origin, "
        "parent_version_id, template_id, library_visible, note, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (version_id, account_id, item_id, next_version_no(conn, item_id), document_version_id, origin,
         parent_version_id, template_id, int(bool(library_visible)), note, created_by, ts(now)))
    conn.execute("UPDATE cv_library_items SET updated_at = ? WHERE id = ?", (ts(now), item_id))
    return get_version(conn, account_id=account_id, version_id=version_id)  # type: ignore[return-value]


def get_version(conn: dbapi.Connection, *, account_id: str, version_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"{_VERSION_SELECT} WHERE v.id = ? AND v.account_id = ?", (version_id, account_id)).fetchone()
    return None if row is None else dict(row)


def list_versions(conn: dbapi.Connection, *, account_id: str, item_id: str,
                  include_hidden: bool = False) -> list[dict[str, Any]]:
    hidden = "" if include_hidden else " AND v.library_visible = 1"
    return [dict(r) for r in conn.execute(f"{_VERSION_SELECT} WHERE v.item_id = ? AND v.account_id = ?{hidden} "
                                          f"ORDER BY v.version_no", (item_id, account_id))]


def latest_visible_version(conn: dbapi.Connection, *, account_id: str, item_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"{_VERSION_SELECT} WHERE v.item_id = ? AND v.account_id = ? AND v.library_visible = 1 "
                       f"ORDER BY v.version_no DESC LIMIT 1", (item_id, account_id)).fetchone()
    return None if row is None else dict(row)


def version_for_document(conn: dbapi.Connection, *, account_id: str, document_version_id: str) -> dict | None:
    row = conn.execute(f"{_VERSION_SELECT} WHERE v.document_version_id = ? AND v.account_id = ? "
                       f"ORDER BY v.seq LIMIT 1", (document_version_id, account_id)).fetchone()
    return None if row is None else dict(row)


def reference_count(conn: dbapi.Connection, document_version_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM document_version_references WHERE document_version_id = ?",
                       (document_version_id,)).fetchone()
    return int(row[0])


def active_item_count(conn: dbapi.Connection, account_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM cv_library_items WHERE account_id = ? AND status = 'ACTIVE'",
                       (account_id,)).fetchone()
    return int(row[0])
