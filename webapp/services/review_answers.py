"""Bundle 6D-A: answer, proposal, disposition and acknowledgement actions
(spec §6.1, §8.2, §8.4). Each commits its DB change together with its
review event. Sensitive subjects are only ever answered for this one
application (Reach.APPLICATION); a system proposal never becomes an answer
without the user accepting it."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from product.autonomy_contract import Reach
from product.review_contract import PlannedField, WarningLevel
from product.semantic_subject_policy import load_subject_policy, subject_entry
from webapp.config import Settings
from webapp.persistence import review_approval as ra
from webapp.persistence.application_identity import get_search_workspace_for_application
from webapp.persistence.artifacts import get_current_artifact
from webapp.persistence.autonomy_answers import approve_answer, current_approved_answers
from webapp.persistence.workspaces import get_workspace
from webapp.services.autonomy_context import employer_identity
from webapp.services.autonomy_controls import run_immediate
from webapp.services.review_application import ReviewRefused, build_reviewable
from webapp.services.review_fields import pending_proposals


def _field(conn, settings: Settings, account_id: str, ws: str, answer_key: str, now: datetime) -> PlannedField:
    reviewable = build_reviewable(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
    field = next((f for f in (reviewable.fields if reviewable else ()) if f.answer_key == answer_key), None)
    if field is None:
        raise ReviewRefused("unknown_field")
    return field


def _answerable(field: PlannedField) -> dict[str, Any]:
    if field.answer_key.startswith("contact:"):
        raise ReviewRefused("evidence_field")  # contact values come from the evidence profile
    entry = subject_entry(load_subject_policy(), field.subject) if field.subject else None
    if entry is None:
        raise ReviewRefused("unclassified_subject")
    return entry


def _scope(conn, account_id: str, ws: str, reach: Reach) -> str | None:
    if reach is Reach.APPLICATION:
        return ws
    if reach is Reach.SEARCH_WORKSPACE:
        return get_search_workspace_for_application(conn, ws)
    if reach is Reach.EMPLOYER:
        workspace = get_workspace(conn, ws, account_id=account_id) or {}
        posting = get_current_artifact(conn, ws, "job_posting_snapshot")
        key, _ = employer_identity((posting or {}).get("payload") or {}, workspace)
        return key
    return None


def _approve(conn, *, account_id: str, ws: str, subject: str, entry: Mapping[str, Any], value: Any, reach: Reach,
             context: Mapping[str, Any] | None, provenance: str, actor: str, now: datetime) -> dict[str, Any]:
    if entry["sensitive"] is not None:
        reach = Reach.APPLICATION  # sensitive: this application only, never a standing answer
    scope_id = _scope(conn, account_id, ws, reach)
    stored_scope = account_id if reach is Reach.ACCOUNT else scope_id
    previous = next((a for a in current_approved_answers(conn, account_id=account_id, subject=subject)
                     if a["reach"] == reach.value and a["scope_id"] == stored_scope), None)
    return approve_answer(conn, account_id=account_id, subject=subject, value=value, reach=reach, scope_id=scope_id,
                          context=dict(context or {}), basis={"kind": "USER_ASSERTION"}, approved_by=actor, now=now,
                          provenance=provenance, supersedes_id=previous["id"] if previous else None, commit=False)


def answer_field(conn, *, settings: Settings, account_id: str, application_workspace_id: str, answer_key: str,
                 value: Any, reach: Reach, actor: str, now: datetime,
                 context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ws = application_workspace_id

    def work() -> dict[str, Any]:
        field = _field(conn, settings, account_id, ws, answer_key, now)
        entry = _answerable(field)
        answer = _approve(conn, account_id=account_id, ws=ws, subject=field.subject, entry=entry, value=value,
                          reach=Reach(reach), context=context, provenance="USER", actor=actor, now=now)
        ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="ANSWER_EDITED",
                        binding_hash=None, detail={"answer_key": answer_key, "approved_answer_id": answer["id"]},
                        actor=actor, now=now)
        return {"approved_answer_id": answer["id"]}
    return run_immediate(conn, work)


def accept_proposal(conn, *, settings: Settings, account_id: str, application_workspace_id: str, proposal_id: str,
                    edited_value: Any, reach: Reach, actor: str, now: datetime,
                    context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ws = application_workspace_id

    def work() -> dict[str, Any]:
        workspace = get_workspace(conn, ws, account_id=account_id)  # ownership before anything is read
        if workspace is None or workspace.get("kind") != "job":
            raise LookupError(ws)
        proposal = conn.execute(
            "SELECT p.* FROM proposed_answers p JOIN application_blockers b ON b.id = p.blocker_id "
            "WHERE p.id = ? AND b.workspace_id = ?", (proposal_id, ws)).fetchone()
        if proposal is None:
            raise LookupError(proposal_id)
        if any(e["event"] == "PROPOSAL_ACCEPTED" and e["detail"].get("proposal_id") == proposal_id
               for e in ra.events(conn, ws)):
            raise ReviewRefused("already_accepted")
        if proposal_id not in {p["id"] for p in pending_proposals(conn, ws)}:
            raise ReviewRefused("stale_proposal")  # its blocker no longer governs this application
        entry = subject_entry(load_subject_policy(), proposal["subject"])
        if entry is None:
            raise ReviewRefused("unclassified_subject")
        value = json.loads(proposal["value_json"]) if edited_value is None else edited_value
        answer = _approve(conn, account_id=account_id, ws=ws, subject=proposal["subject"], entry=entry, value=value,
                          reach=Reach(reach), context=context, provenance="USER_EDITED_PROPOSAL", actor=actor,
                          now=now)
        ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="PROPOSAL_ACCEPTED",
                        binding_hash=None, detail={"proposal_id": proposal_id, "approved_answer_id": answer["id"]},
                        actor=actor, now=now)
        return {"approved_answer_id": answer["id"]}
    return run_immediate(conn, work)


def set_field_disposition(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                          answer_key: str, disposition: str, actor: str, now: datetime) -> dict[str, Any]:
    ws = application_workspace_id
    if disposition not in ("ANSWER", "OMIT"):
        raise ReviewRefused("invalid_disposition")

    def work() -> dict[str, Any]:
        field = _field(conn, settings, account_id, ws, answer_key, now)
        if disposition == "OMIT" and field.required:
            raise ReviewRefused("required_field")
        if disposition == "ANSWER" and field.subject is None:
            raise ReviewRefused("unclassified_subject")
        row = ra.set_disposition(conn, account_id=account_id, application_workspace_id=ws, answer_key=answer_key,
                                 disposition=disposition, actor=actor, now=now)
        ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="FIELD_DISPOSITION_SET",
                        binding_hash=None, detail={"answer_key": answer_key, "disposition": disposition},
                        actor=actor, now=now)
        return {"disposition_id": row["id"]}
    return run_immediate(conn, work)


def acknowledge_warning(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                        warning_key: str, actor: str, now: datetime) -> dict[str, Any]:
    """Only a current ATTENTION warning, by its exact (fingerprinted) key."""
    ws = application_workspace_id

    def work() -> dict[str, Any]:
        reviewable = build_reviewable(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                                      now=now)
        warning = next((w for w in (reviewable.warnings if reviewable else ()) if w.key == warning_key), None)
        if warning is None or warning.level is not WarningLevel.ATTENTION:
            raise ReviewRefused("not_attention")
        event = ra.record_event(conn, account_id=account_id, application_workspace_id=ws,
                                event="WARNING_ACKNOWLEDGED", binding_hash=None, detail={"warning_key": warning_key},
                                actor=actor, now=now)
        return {"event_id": event["id"]}
    return run_immediate(conn, work)
