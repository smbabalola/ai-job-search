# webapp/persistence/review_approval.py
"""Append-only review/approval history (6D-A spec §13-14). No function
commits: callers own the transaction. Current state is derived by seq."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from product.autonomy_contract import canonical_json, to_utc_iso


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn, table: str, values: dict[str, Any]) -> dict[str, Any]:
    cols = ", ".join(values)
    return dict(conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in values)}) RETURNING *",
                            tuple(values.values())).fetchone())


def _approval(row) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["binding"] = json.loads(out["binding_json"])
    out["resolved_delta_ids"] = json.loads(out["resolved_delta_ids_json"])
    return out


def insert_approval(conn, *, account_id: str, application_workspace_id: str, binding: dict[str, Any],
                    binding_hash: str, supersedes_id: str | None, batch_id: str | None,
                    resolved_delta_ids: list[str], actor: str, now: datetime) -> dict[str, Any]:
    row = _insert(conn, "application_approvals", {
        "id": _id("apr"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "scope": "FILL", "binding_json": canonical_json(binding), "binding_hash": binding_hash,
        "supersedes_id": supersedes_id, "batch_id": batch_id,
        "resolved_delta_ids_json": json.dumps(sorted(resolved_delta_ids)), "actor": actor,
        "created_at": to_utc_iso(now)})
    # Bundle 7 L3: the approved documents can never be deleted while this approval exists.
    from webapp.persistence.cv_library import add_references
    from webapp.persistence.bundle7_migrations import binding_documents
    add_references(conn, document_version_ids=binding_documents(binding), referrer_type="APPROVAL",
                   referrer_id=row["id"], now=now)
    return _approval(row)


def get_approval(conn, approval_id: str) -> dict[str, Any] | None:
    return _approval(conn.execute("SELECT * FROM application_approvals WHERE id = ?", (approval_id,)).fetchone())


def latest_approval(conn, ws: str) -> dict[str, Any] | None:
    return _approval(conn.execute("SELECT * FROM application_approvals WHERE application_workspace_id = ? "
                                  "ORDER BY seq DESC LIMIT 1", (ws,)).fetchone())


def record_event(conn, *, account_id: str, application_workspace_id: str, event: str, binding_hash: str | None,
                 detail: dict[str, Any], actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "application_review_events", {
        "id": _id("revt"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "event": event, "binding_hash": binding_hash, "detail_json": canonical_json(detail), "actor": actor,
        "created_at": to_utc_iso(now)})


def events(conn, ws: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM application_review_events WHERE application_workspace_id = ? ORDER BY seq",
                        (ws,)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]


def _events_of(conn, ws: str, event: str) -> list[dict[str, Any]]:
    return [e for e in events(conn, ws) if e["event"] == event]


def approval_revoked(conn, approval: dict[str, Any]) -> bool:
    return any(e["detail"].get("approval_id") == approval["id"]
               for e in _events_of(conn, approval["application_workspace_id"], "REVOKED"))


def presented_at(conn, ws: str, binding_hash: str) -> bool:
    return any(e["binding_hash"] == binding_hash for e in _events_of(conn, ws, "REVIEW_PRESENTED"))


def acknowledged_warning_keys(conn, ws: str) -> frozenset[str]:
    return frozenset(e["detail"]["warning_key"] for e in _events_of(conn, ws, "WARNING_ACKNOWLEDGED"))


def insert_delta(conn, *, account_id: str, application_workspace_id: str, kind: str, answer_key: str | None,
                 subject: str | None, required: bool, question: str, observed: dict[str, Any], source: str,
                 now: datetime) -> dict[str, Any]:
    return _insert(conn, "review_deltas", {
        "id": _id("dlt"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "kind": kind, "answer_key": answer_key, "subject": subject, "required": 1 if required else 0,
        "question": question, "observed_json": canonical_json(observed), "source": source,
        "created_at": to_utc_iso(now)})



def list_deltas(conn, ws: str) -> list[dict[str, Any]]:
    """Every delta of the application, open or resolved, by seq."""
    rows = conn.execute("SELECT * FROM review_deltas WHERE application_workspace_id = ? ORDER BY seq",
                        (ws,)).fetchall()
    return [{**dict(r), "observed": json.loads(r["observed_json"])} for r in rows]

def open_deltas(conn, ws: str) -> list[dict[str, Any]]:
    resolved = {e["detail"].get("delta_id") for e in _events_of(conn, ws, "DELTA_RESOLVED")}
    rows = conn.execute("SELECT * FROM review_deltas WHERE application_workspace_id = ? ORDER BY seq",
                        (ws,)).fetchall()
    return [{**dict(r), "observed": json.loads(r["observed_json"])} for r in rows if r["id"] not in resolved]


def set_disposition(conn, *, account_id: str, application_workspace_id: str, answer_key: str, disposition: str,
                    actor: str, now: datetime) -> dict[str, Any]:
    return _insert(conn, "application_field_dispositions", {
        "id": _id("disp"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "answer_key": answer_key, "disposition": disposition, "actor": actor, "created_at": to_utc_iso(now)})


def current_dispositions(conn, ws: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in conn.execute("SELECT answer_key, disposition FROM application_field_dispositions "
                          "WHERE application_workspace_id = ? ORDER BY seq", (ws,)).fetchall():
        out[r["answer_key"]] = r["disposition"]
    return out


def invalidation_recorded(conn, ws: str, approval_id: str, reasons: list[str]) -> bool:
    """Once per approval per exact reason set (spec §13): the dedup key is the
    sorted reason set only, never a binding/provisional hash."""
    key = sorted(set(reasons))
    return any(e["detail"].get("approval_id") == approval_id and sorted(set(e["detail"].get("reasons", []))) == key
               for e in _events_of(conn, ws, "APPROVAL_INVALIDATED"))
