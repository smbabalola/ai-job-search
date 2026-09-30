"""Account policy documents and CV resolutions (Bundle 7 spec §14.1). Both are
append-only; the current policy document is the max seq per (account, type)."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from webapp.persistence import dbapi

SCHEMA_VERSIONS = {"job-families": "job-families.v1", "cv-strategy": "cv-strategy.v1"}


def ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def canonical(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def doc_hash(doc: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(doc).encode("utf-8")).hexdigest()


def save_account_document(conn: dbapi.Connection, *, account_id: str, doc_type: str, doc: dict, created_by: str,
                          now: datetime) -> dict[str, Any]:
    """No commit; the caller validated ``doc``."""
    if doc_type not in SCHEMA_VERSIONS:
        raise ValueError(f"unknown account document type {doc_type}")
    row_id = f"apd_{uuid.uuid4().hex[:20]}"
    digest = doc_hash(doc)
    conn.execute("INSERT INTO account_policy_documents (id, account_id, doc_type, schema_version, doc_json, doc_hash, "
                 "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (row_id, account_id, doc_type, SCHEMA_VERSIONS[doc_type], canonical(doc), digest, created_by,
                  ts(now)))
    return {"id": row_id, "doc_hash": digest}


def current_account_document(conn: dbapi.Connection, *, account_id: str, doc_type: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM account_policy_documents WHERE account_id = ? AND doc_type = ? "
                       "ORDER BY seq DESC LIMIT 1", (account_id, doc_type)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["doc"] = json.loads(out.pop("doc_json"))
    return out


def insert_resolution(conn: dbapi.Connection, *, account_id: str, workspace_id: str, job_families_hash: str | None,
                      cv_strategy_hash: str | None, family_id: str, family_match: dict, rule: dict, outcome: str,
                      item_id: str | None, version_id: str | None, overridden_by_user: bool,
                      now: datetime) -> dict[str, Any]:
    row_id = f"cvr_{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO application_cv_resolutions (id, account_id, application_workspace_id, job_families_hash, "
        "cv_strategy_hash, family_id, family_match_json, rule_json, outcome, item_id, version_id, overridden_by_user, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, account_id, workspace_id, job_families_hash, cv_strategy_hash, family_id, canonical(family_match),
         canonical(rule), outcome, item_id, version_id, int(overridden_by_user), ts(now)))
    return dict(conn.execute("SELECT * FROM application_cv_resolutions WHERE id = ?", (row_id,)).fetchone())


def latest_resolution(conn: dbapi.Connection, workspace_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM application_cv_resolutions WHERE application_workspace_id = ? "
                       "ORDER BY seq DESC LIMIT 1", (workspace_id,)).fetchone()
    return None if row is None else dict(row)
