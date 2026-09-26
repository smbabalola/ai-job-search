"""Approved-answer library and related user acts (6B spec §7.5, §8.1, §9.3).
Approved answers are immutable; editing supersedes. Freshness is anchored at
the latest answer_confirmations row by seq."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Mapping

from product.autonomy_contract import REACH_ORDER, Reach, CanonicalHashError, canonical_json, to_utc_iso
from product.semantic_subject_policy import load_subject_policy, subject_entry


class AnswerValidationError(ValueError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _insert(conn, table: str, values: dict[str, Any]) -> dict[str, Any]:
    cols, marks = ", ".join(values), ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return dict(conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (cur.lastrowid,)).fetchone())


def _validate(subject: str, reach: Reach, scope_id: str | None, context: Mapping[str, Any],
              basis: Mapping[str, Any], policy: Mapping[str, Any], provenance: str = "USER",
              supersedes_id: str | None = None, conn: sqlite3.Connection | None = None,
              account_id: str | None = None) -> dict[str, Any]:
    entry = subject_entry(policy, subject)
    if entry is None:
        raise AnswerValidationError(f"unknown subject {subject!r}")
    if entry["sensitive"] is not None:
        raise AnswerValidationError(f"sensitive subject {subject!r} cannot have a standing answer in v1")
    if REACH_ORDER[reach] > REACH_ORDER[Reach(entry["max_reach"])]:
        raise AnswerValidationError(f"reach {reach.value} exceeds max_reach {entry['max_reach']} for {subject}")
    if reach is not Reach.ACCOUNT and not scope_id:
        raise AnswerValidationError(f"scope_id is required for reach {reach.value}")
    extra = set(context) - set(entry["context_keys"])
    if extra:
        raise AnswerValidationError(f"context keys {sorted(extra)} are not declared for {subject}")
    if basis.get("kind") == "USER_ASSERTION":
        if set(basis) != {"kind"}:
            raise AnswerValidationError("USER_ASSERTION basis takes no other keys")
    elif basis.get("kind") == "EVIDENCE":
        if set(basis) != {"kind", "evidence_ids", "value_hash"} or not basis["evidence_ids"]:
            raise AnswerValidationError("EVIDENCE basis needs evidence_ids and value_hash")
        value_hash = basis.get("value_hash")
        if not isinstance(value_hash, str) or not value_hash.startswith("sha256:"):
            raise AnswerValidationError(f"EVIDENCE basis value_hash must be a string starting with 'sha256:', got {value_hash!r}")
    else:
        raise AnswerValidationError("basis kind must be USER_ASSERTION or EVIDENCE")
    if provenance not in ("USER", "USER_EDITED_PROPOSAL"):
        raise AnswerValidationError(f"provenance must be USER or USER_EDITED_PROPOSAL, got {provenance!r}")
    if supersedes_id and conn and account_id:
        prev = conn.execute(
            "SELECT account_id, subject FROM approved_answers WHERE id = ?",
            (supersedes_id,)
        ).fetchone()
        if prev is None:
            raise AnswerValidationError(f"supersedes_id {supersedes_id!r} does not exist")
        if prev["account_id"] != account_id:
            raise AnswerValidationError(f"supersedes_id {supersedes_id!r} belongs to different account")
        if prev["subject"] != subject:
            raise AnswerValidationError(f"supersedes_id {supersedes_id!r} has different subject")
    return entry


def approve_answer(conn: sqlite3.Connection, *, account_id: str, subject: str, value: Any, reach: Reach,
                   scope_id: str | None, context: dict[str, Any], basis: dict[str, Any], approved_by: str,
                   now: datetime, provenance: str = "USER", basis_profile_version_id: str | None = None,
                   supersedes_id: str | None = None, source_blocker_resolution_id: str | None = None,
                   subject_policy: Mapping[str, Any] | None = None, commit: bool = True) -> dict[str, Any]:
    try:
        entry = _validate(subject, Reach(reach), scope_id, context, basis, subject_policy or load_subject_policy(),
                         provenance=provenance, supersedes_id=supersedes_id, conn=conn, account_id=account_id)
    except CanonicalHashError as e:
        raise AnswerValidationError(str(e)) from e

    conn.execute("SAVEPOINT approve_answer")
    try:
        try:
            answer = _insert(conn, "approved_answers", {
                "id": _id("ans"), "account_id": account_id, "subject": subject,
                "answer_kind": entry["answer_kind"], "value_json": canonical_json(value),
                "reach": Reach(reach).value, "scope_id": account_id if Reach(reach) is Reach.ACCOUNT else scope_id,
                "context_json": canonical_json(context), "provenance": provenance,
                "basis_json": canonical_json(basis), "basis_profile_version_id": basis_profile_version_id,
                "supersedes_id": supersedes_id, "source_blocker_resolution_id": source_blocker_resolution_id,
                "approved_by": approved_by, "created_at": to_utc_iso(now),
            })
            _insert(conn, "answer_confirmations", {
                "id": _id("conf"), "approved_answer_id": answer["id"], "confirmed_by": approved_by,
                "created_at": to_utc_iso(now),
            })
        except CanonicalHashError as e:
            raise AnswerValidationError(str(e)) from e
        conn.execute("RELEASE approve_answer")
    except Exception:
        conn.execute("ROLLBACK TO approve_answer")
        conn.execute("RELEASE approve_answer")
        raise

    if commit:
        conn.commit()
    return answer


def confirm_answer(conn, *, approved_answer_id: str, confirmed_by: str, now: datetime,
                   commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "answer_confirmations", {
        "id": _id("conf"), "approved_answer_id": approved_answer_id, "confirmed_by": confirmed_by,
        "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def current_approved_answers(conn, *, account_id: str, subject: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.*, ("
        "  SELECT c.created_at FROM answer_confirmations c WHERE c.approved_answer_id = a.id "
        "  ORDER BY c.seq DESC LIMIT 1) AS latest_confirmation_at, ("
        "  SELECT c.id FROM answer_confirmations c WHERE c.approved_answer_id = a.id "
        "  ORDER BY c.seq DESC LIMIT 1) AS latest_confirmation_id "
        "FROM approved_answers a WHERE a.account_id = ? AND a.subject = ? "
        "AND NOT EXISTS (SELECT 1 FROM approved_answers s WHERE s.supersedes_id = a.id) "
        "ORDER BY a.seq",
        (account_id, subject),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["value"] = json.loads(item["value_json"])
        item["context"] = json.loads(item["context_json"])
        item["basis"] = json.loads(item["basis_json"])
        out.append(item)
    return out


def save_proposed_answer(conn, *, blocker_id: str, subject: str, value: Any, now: datetime,
                         commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "proposed_answers", {
        "id": _id("prop"), "blocker_id": blocker_id, "subject": subject,
        "value_json": canonical_json(value), "provenance": "SYSTEM_PROPOSED", "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def record_rule_acknowledgement(conn, *, account_id: str, application_workspace_id: str, rule_id: str,
                                rule_hash: str, observed_fingerprint: str, policy_version_hash: str,
                                disposition: str, actor: str, now: datetime, commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "rule_acknowledgements", {
        "id": _id("ack"), "account_id": account_id, "application_workspace_id": application_workspace_id,
        "rule_id": rule_id, "rule_hash": rule_hash, "observed_fingerprint": observed_fingerprint,
        "policy_version_hash": policy_version_hash, "disposition": disposition, "actor": actor,
        "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def current_rule_acknowledgements(conn, application_workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.* FROM rule_acknowledgements r WHERE r.application_workspace_id = ? AND r.seq = ("
        "  SELECT MAX(x.seq) FROM rule_acknowledgements x "
        "  WHERE x.application_workspace_id = r.application_workspace_id AND x.rule_id = r.rule_id) "
        "ORDER BY r.rule_id",
        (application_workspace_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def confirm_apply_target(conn, *, application_workspace_id: str, job_identity_key: str | None,
                         canonical_url: str, confirmed_by: str, now: datetime, commit: bool = True) -> dict[str, Any]:
    row = _insert(conn, "apply_target_confirmations", {
        "id": _id("atc"), "application_workspace_id": application_workspace_id,
        "job_identity_key": job_identity_key, "canonical_url": canonical_url,
        "confirmed_by": confirmed_by, "created_at": to_utc_iso(now),
    })
    if commit:
        conn.commit()
    return row


def current_apply_target_confirmation(conn, application_workspace_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM apply_target_confirmations WHERE application_workspace_id = ? ORDER BY seq DESC LIMIT 1",
        (application_workspace_id,),
    ).fetchone()
    return dict(row) if row else None
