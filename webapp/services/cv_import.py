"""CV import (Bundle 7 spec §15.3): a CV version → text → a metered extraction
(profile.cv_import) → validated proposals. Nothing is written to the profile
or the answers until the user resolves a proposal; resolving several at once
is one profile_manager write (a single source revision). Accepted entries
carry the provenance ``cv_import:<document_version_id>``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from product.cv_extraction import build_request, validate_proposals
from product.semantic_subject_registry import SEMANTIC_SUBJECTS
from webapp.persistence import dbapi

__all__ = ["ImportError_", "ImportFailed", "import_cv", "list_proposals", "resolve_batch", "resolve_proposal"]

ENTRY_KINDS = ("employment", "education", "certification", "technical_skill", "language", "achievement")
RESOLUTIONS = ("ACCEPTED", "EDITED_ACCEPTED", "REJECTED")


class ImportError_(Exception):
    """Not found in this account, or already resolved."""


class ImportFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _entry_fields() -> dict[str, tuple[str, ...]]:
    from webapp.services.profile_manager import ENTRY_DEFINITIONS
    return {kind: tuple(ENTRY_DEFINITIONS[kind]["fields"]) for kind in ENTRY_KINDS}


def _run(conn: dbapi.Connection, run_id: str) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM profile_import_runs WHERE id = ?", (run_id,)).fetchone())


def _finish(conn: dbapi.Connection, run_id: str, *, status: str, now: datetime, error_code: str | None = None,
            detail: dict | None = None) -> None:
    conn.execute("UPDATE profile_import_runs SET status = ?, error_code = ?, detail_json = ?, completed_at = ? "
                 "WHERE id = ?", (status, error_code, json.dumps(detail or {}, sort_keys=True), ts(now), run_id))


def _extract_and_propose(conn: dbapi.Connection, scope: Any, *, run_id: str, document_version_id: str, provider: Any,
                         documents_root: Any, now: datetime) -> dict[str, Any]:
    from webapp.persistence.application_documents import get_document_version
    from webapp.services.cv_text import extract_text
    from webapp.services.document_blob_store import DocumentBlobStore
    document = get_document_version(conn, document_version_id, account_id=scope.account_id)
    content = DocumentBlobStore(documents_root).read(document)
    try:
        text = extract_text(content, document["media_type"])
    except Exception:  # noqa: BLE001 - an unreadable file is a failed import, not a crash
        raise ImportFailed("UNREADABLE") from None
    if not text.strip():
        raise ImportFailed("NO_TEXT")
    entry_fields = _entry_fields()
    request = build_request(text, request_id=run_id, entry_fields=entry_fields, answer_subjects=SEMANTIC_SUBJECTS)
    response = provider.extract(request)
    valid, dropped = validate_proposals(getattr(response, "payload", None), entry_fields=entry_fields,
                                        answer_subjects=SEMANTIC_SUBJECTS)
    for proposal in valid:
        conn.execute("INSERT INTO profile_proposals (id, account_id, import_run_id, target, kind, fields_json, "
                     "source_excerpt, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (f"pp_{uuid.uuid4().hex[:20]}", scope.account_id, run_id, proposal["target"], proposal["kind"],
                      json.dumps(proposal["fields"], sort_keys=True), proposal["source_excerpt"],
                      proposal["confidence"], ts(now)))
    _finish(conn, run_id, status="PROPOSED", now=now,
            detail={"proposed": len(valid), "dropped": dropped, "truncated": request["truncated"]})
    return _run(conn, run_id)


def import_cv(conn: dbapi.Connection, scope: Any, *, document_version_id: str, metering: Any, provider: Any,
              documents_root: Any, now: datetime) -> dict[str, Any]:
    """Create a run and extract now, metered as one profile.cv_import (released on failure).
    ``provider`` arrives already behind the metered AI boundary (the route wraps it)."""
    from webapp.persistence.application_documents import get_document_version
    document = get_document_version(conn, document_version_id, account_id=scope.account_id)
    if document is None or document["document_kind"] != "cv":
        raise ImportError_("CV not found")
    run_id = f"pir_{uuid.uuid4().hex[:20]}"
    conn.execute("INSERT INTO profile_import_runs (id, account_id, document_version_id, status, created_at) "
                 "VALUES (?, ?, ?, 'RUNNING', ?)", (run_id, scope.account_id, document_version_id, ts(now)))
    conn.commit()

    def work() -> dict[str, Any]:
        return _extract_and_propose(conn, scope, run_id=run_id, document_version_id=document_version_id,
                                    provider=provider, documents_root=documents_root, now=now)

    try:
        if metering is None or not metering.enforced:
            result = work()
            conn.commit()
            return result
        return metering.metered(conn, scope, feature="profile.cv_import", allowance="profile.cv_import",
                                subject_type="profile_import_run", subject_id=run_id,
                                key=lambda window_key: f"cv_import:{run_id}", action=f"cv_import:{document_version_id}",
                                work=work)
    except ImportFailed as failure:
        if conn.in_transaction:
            conn.rollback()
        _finish(conn, run_id, status="FAILED", now=now, error_code=failure.code)
        conn.commit()
        return _run(conn, run_id)


def list_proposals(conn: dbapi.Connection, scope: Any, *, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.*, r.resolution FROM profile_proposals p LEFT JOIN profile_proposal_resolutions r "
        "ON r.proposal_id = p.id WHERE p.import_run_id = ? AND p.account_id = ? ORDER BY p.seq",
        (run_id, scope.account_id)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["fields"] = json.loads(item.pop("fields_json"))
        out.append(item)
    return out


def _owned_open_proposal(conn: dbapi.Connection, scope: Any, proposal_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT p.*, r.document_version_id FROM profile_proposals p JOIN profile_import_runs r "
                       "ON r.id = p.import_run_id WHERE p.id = ? AND p.account_id = ?",
                       (proposal_id, scope.account_id)).fetchone()
    if row is None:
        raise ImportError_("proposal not found")
    if conn.execute("SELECT 1 FROM profile_proposal_resolutions WHERE proposal_id = ?", (proposal_id,)).fetchone():
        raise ImportError_("proposal already resolved")
    proposal = dict(row)
    proposal["fields"] = json.loads(proposal.pop("fields_json"))
    return proposal


def _record(conn: dbapi.Connection, *, proposal_id: str, resolution: str, final: dict | None, ref: str | None,
            actor: str, now: datetime) -> dict[str, Any]:
    row_id = f"ppr_{uuid.uuid4().hex[:20]}"
    conn.execute("INSERT INTO profile_proposal_resolutions (id, proposal_id, resolution, final_fields_json, "
                 "resulting_ref, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (row_id, proposal_id, resolution, json.dumps(final, sort_keys=True) if final is not None else None,
                  ref, actor, ts(now)))
    return dict(conn.execute("SELECT * FROM profile_proposal_resolutions WHERE id = ?", (row_id,)).fetchone())


def _entry_payload(fields: dict[str, Any]) -> dict[str, Any]:
    """Proposal fields are strings; the profile entry's ``details`` is a list of lines."""
    out = dict(fields)
    if isinstance(out.get("details"), str):
        import re
        out["details"] = [part.strip() for part in re.split(r"[\n;]+", out["details"]) if part.strip()]
    return out


def resolve_batch(conn: dbapi.Connection, scope: Any, *, items: list[tuple[str, str, dict | None]],
                  now: datetime) -> list[dict[str, Any]]:
    """Resolve proposals; profile entries go in ONE profile_manager write."""
    from webapp.persistence.autonomy_answers import approve_answer
    from webapp.services.profile_manager import create_profile_entries, get_profile_manager
    from product.autonomy_contract import Reach
    actor = getattr(scope, "user_id", None) or scope.account_id
    plan = []
    for proposal_id, resolution, fields in items:
        if resolution not in RESOLUTIONS:
            raise ImportError_(f"unknown resolution {resolution}")
        proposal = _owned_open_proposal(conn, scope, proposal_id)
        final_fields = dict(fields) if resolution == "EDITED_ACCEPTED" and fields else proposal["fields"]
        plan.append((proposal, resolution, final_fields))
    entries = [(p, fields) for p, resolution, fields in plan
               if resolution != "REJECTED" and p["target"] == "PROFILE_ENTRY"]
    entry_ids: dict[str, str] = {}
    if entries:
        root = scope.profile_sources(conn)
        manager = get_profile_manager(conn, root=root, account_id=scope.account_id)
        created = create_profile_entries(conn, root=root, expected_revision=manager["revision"],
                                         entries=[(p["kind"], _entry_payload(fields)) for p, fields in entries],
                                         account_id=scope.account_id)
        entry_ids = {p["id"]: entry_id for (p, _), entry_id in zip(entries, created["entry_ids"])}
    out = []
    for proposal, resolution, fields in plan:
        provenance = f"cv_import:{proposal['document_version_id']}"
        if resolution == "REJECTED":
            out.append(_record(conn, proposal_id=proposal["id"], resolution=resolution, final=None, ref=None,
                               actor=actor, now=now))
            continue
        if proposal["target"] == "PROFILE_ENTRY":
            ref = entry_ids[proposal["id"]]
        else:
            answer = approve_answer(conn, account_id=scope.account_id, subject=proposal["kind"],
                                    value={"value": fields.get("value")}, reach=Reach.ACCOUNT, scope_id=None,
                                    context={}, basis={"kind": "USER_ASSERTION"},
                                    approved_by=actor, now=now, commit=False)
            ref = answer["id"]
        out.append(_record(conn, proposal_id=proposal["id"], resolution=resolution,
                           final={"fields": fields, "provenance": provenance}, ref=ref, actor=actor, now=now))
    conn.commit()
    return out


def resolve_proposal(conn: dbapi.Connection, scope: Any, *, proposal_id: str, resolution: str,
                     fields: dict | None = None, now: datetime) -> dict[str, Any]:
    return resolve_batch(conn, scope, items=[(proposal_id, resolution, fields)], now=now)[0]
