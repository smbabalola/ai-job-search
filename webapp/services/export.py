"""Data export (Bundle 7 spec §20.5).

``request_export`` queues one export (one in flight per account, at most 3 a
day); the worker's ``account.export`` runs ``build_export``: a ZIP in the
object store at ``accounts/{id}/exports/{export_id}.zip`` with one JSON file
per export-class table (the account's rows, secret columns removed), the
original bytes of every document version (``documents/``) and the current
profile sources (``profile/``). The link expires per DP-4 (default 7 days)
and downloading needs the account's signed-in session."""
from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from datetime import datetime, timedelta
from typing import Any

from webapp.persistence import dbapi
from webapp.persistence.tenancy import TENANT_TABLES
from webapp.services.purge import account_filter

__all__ = ["DAILY_LIMIT", "ExportRefused", "SECRET_COLUMN", "build_export", "export_tables", "read_export",
           "request_export"]

DAILY_LIMIT = 3
# Never exported: hashes of credentials, tokens, secrets, passwords.
SECRET_COLUMN = re.compile(r"(_hash$|^token|_token$|secret|password|credential)")


class ExportRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def export_tables(conn: dbapi.Connection) -> list[str]:
    from webapp.persistence.schema_catalog import schema_catalog
    catalog = schema_catalog(conn)
    return [t for t, spec in TENANT_TABLES.items() if spec.export and spec.owner != "GLOBAL" and t in catalog]


def request_export(conn: dbapi.Connection, scope: Any, *, now: datetime, settings: Any) -> str:
    """Commits. Returns the export id."""
    from webapp.persistence.audit import audit
    from webapp.worker.runner import enqueue
    with dbapi.account_transaction(conn, scope.account_id):
        if conn.execute("SELECT 1 FROM account_exports WHERE account_id = ? AND status = 'QUEUED'",
                        (scope.account_id,)).fetchone():
            raise ExportRefused("ACTION_IN_PROGRESS", "An export is already being prepared.")
        today = conn.execute("SELECT COUNT(*) FROM account_exports WHERE account_id = ? AND created_at >= ?",
                             (scope.account_id, _iso(now - timedelta(days=1)))).fetchone()[0]
        if today >= DAILY_LIMIT:
            raise ExportRefused("RATE_LIMITED", f"You can export your data {DAILY_LIMIT} times a day.")
        export_id = f"exp_{uuid.uuid4().hex[:20]}"
        conn.execute("INSERT INTO account_exports (id, account_id, requested_by, status, created_at) "
                     "VALUES (?, ?, ?, 'QUEUED', ?)", (export_id, scope.account_id, scope.user_id, _iso(now)))
        enqueue(conn, kind="account.export", account_id=scope.account_id, payload={"export_id": export_id},
                dedupe_key=f"export:{export_id}", now=now)
        audit(conn, actor_type="USER", actor_id=scope.user_id, account_id=scope.account_id,
              action="DATA_EXPORT_REQUESTED", now=now, target_type="export", target_id=export_id,
              secret=settings.secret_key)
    return export_id


def _rows(conn: dbapi.Connection, table: str, account_id: str, user_ids: list[str]) -> list[dict[str, Any]]:
    where, params = account_filter(table, account_id, user_ids)
    out = []
    for row in conn.execute(f'SELECT * FROM "{table}" WHERE {where}', tuple(params)).fetchall():
        out.append({k: row[k] for k in row.keys() if not SECRET_COLUMN.search(k)})
    return out


def build_export(conn: dbapi.Connection, *, export_id: str, object_store: Any, now: datetime, settings: Any,
                 policy: Any) -> None:
    """Commits. Idempotent: a READY export is left alone."""
    from webapp.services.document_blob_store import DocumentBlobStore
    from webapp.services.notifications import notify
    export = conn.execute("SELECT * FROM account_exports WHERE id = ?", (export_id,)).fetchone()
    if export is None or export["status"] != "QUEUED":
        return
    account_id = export["account_id"]
    user_ids = [r[0] for r in conn.execute("SELECT user_id FROM account_memberships WHERE account_id = ?",
                                           (account_id,)).fetchall()]
    blobs = DocumentBlobStore(object_store)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        manifest = {"export_id": export_id, "account_id": account_id, "created_at": _iso(now), "tables": {}}
        for table in export_tables(conn):
            rows = _rows(conn, table, account_id, user_ids)
            manifest["tables"][table] = len(rows)
            archive.writestr(f"tables/{table}.json", json.dumps(rows, indent=1, sort_keys=True, default=str))
        # every document version's bytes (CV library versions point at these rows)
        if "application_document_versions" in manifest["tables"]:
            for row in _rows(conn, "application_document_versions", account_id, user_ids):
                document = {"storage_key": row["storage_key"], "byte_length": row["byte_length"],
                            "sha256": row["sha256"]}
                name = re.sub(r"[^A-Za-z0-9._-]", "_", row["original_filename"])
                archive.writestr(f"documents/{row['id']}-{name}", blobs.read(document))
        sources = conn.execute(
            "SELECT r.source_path, r.content FROM profile_source_revisions r WHERE r.account_id = ? AND r.content "
            "IS NOT NULL AND r.revision = (SELECT MAX(revision) FROM profile_source_revisions x WHERE "
            "x.account_id = r.account_id AND x.source_path = r.source_path)", (account_id,)).fetchall()
        for source_path, content in sources:
            archive.writestr(f"profile/{source_path.lstrip('./')}", content)
        archive.writestr("manifest.json", json.dumps(manifest, indent=1, sort_keys=True))
    data = buffer.getvalue()
    key = f"accounts/{account_id}/exports/{export_id}.zip"
    object_store.put(key, data)
    with dbapi.account_transaction(conn, account_id):
        conn.execute("UPDATE account_exports SET status = 'READY', object_key = ?, byte_length = ?, ready_at = ?, "
                     "expires_at = ? WHERE id = ?",
                     (key, len(data), _iso(now), _iso(now + policy.export_link), export_id))
        notify(conn, account_id=account_id, kind="account.data_export_ready", subject_type="export",
               subject_id=export_id, dedupe_key=f"account.data_export_ready:{export_id}",
               detail={"expires_on": (now + policy.export_link).date().isoformat()}, now=now)


def read_export(conn: dbapi.Connection, scope: Any, export_id: str, *, object_store: Any, now: datetime,
                settings: Any) -> bytes:
    """Commits (the download is audited). The export must be the scope's, READY and unexpired."""
    from webapp.persistence.audit import audit
    row = conn.execute("SELECT * FROM account_exports WHERE id = ? AND account_id = ?",
                       (export_id, scope.account_id)).fetchone()
    if row is None or row["status"] != "READY":
        raise ExportRefused("NOT_FOUND", "No such export.")
    if row["expires_at"] <= _iso(now):
        raise ExportRefused("EXPIRED", "This export link has expired. Request a new export.")
    data = object_store.get(row["object_key"])
    with dbapi.account_transaction(conn, scope.account_id):
        conn.execute("UPDATE account_exports SET downloaded_at = ? WHERE id = ?", (_iso(now), export_id))
        audit(conn, actor_type="USER", actor_id=scope.user_id, account_id=scope.account_id,
              action="DATA_EXPORT_DOWNLOADED", now=now, target_type="export", target_id=export_id,
              secret=settings.secret_key)
    return data
