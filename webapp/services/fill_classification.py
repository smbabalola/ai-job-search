"""Bundle 6D-B R7 classification confirmation (spec §9).

A proposal (heuristic or model) never classifies. The user confirms that an
unclassified 6D-B field delta means the displayed proposal's registered
subject; in ONE transaction that opens the classified successor through the
6D-A intake (which resolves the predecessor {reason: classified}) and records
delta_classification_confirmations. Retries write nothing. Answering the
question stays a separate 6D-A decision."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from product.review_contract import FIELD_DELTA_KINDS
from product.semantic_subject_registry import SEMANTIC_SUBJECTS
from webapp.persistence import fill as f
from webapp.persistence import review_approval as ra
from webapp.persistence.workspaces import get_workspace
from webapp.services.autonomy_controls import run_immediate
from webapp.services.review_application import ReviewRefused
from webapp.services.review_approval import open_review_delta_in_transaction


def confirm_classification(conn, *, account_id: str, application_workspace_id: str, delta_id: str,
                           displayed_proposal_id: str, subject: str, actor: str, now: datetime) -> dict[str, Any]:
    ws = application_workspace_id

    def work() -> dict[str, Any]:
        workspace = get_workspace(conn, ws, account_id=account_id)
        if workspace is None or workspace.get("kind") != "job":
            raise LookupError(ws)
        deltas = {d["id"]: d for d in ra.list_deltas(conn, ws)}
        existing = f.classification_confirmation(conn, delta_id)
        if existing is not None:  # a retry: return the recorded successor, write nothing
            if existing["subject"] != subject:
                raise ReviewRefused("already_classified")
            return deltas[existing["successor_delta_id"]]
        delta = deltas.get(delta_id)
        if delta is None:
            raise LookupError(delta_id)
        open_ids = {d["id"] for d in ra.open_deltas(conn, ws)}
        if delta_id not in open_ids or delta["kind"] not in FIELD_DELTA_KINDS or delta["subject"] is not None:
            raise ReviewRefused("not_unclassified")
        proposal = f.latest_proposal(conn, delta_id)
        if proposal is None or proposal["id"] != displayed_proposal_id:
            raise ReviewRefused("stale_proposal")
        if subject != proposal["subject"] or subject not in SEMANTIC_SUBJECTS:
            raise ReviewRefused("subject_mismatch")
        successor = open_review_delta_in_transaction(
            conn, account_id=account_id, application_workspace_id=ws, kind=delta["kind"], answer_key=None,
            subject=subject, required=bool(delta["required"]), question=delta["question"],
            observed=dict(delta["observed"]), source=delta["source"], now=now)
        f.insert_classification_confirmation(conn, account_id=account_id, application_workspace_id=ws,
                                             delta_id=delta_id, proposal_id=proposal["id"], subject=subject,
                                             successor_delta_id=successor["id"], actor=actor, now=now)
        return successor
    return run_immediate(conn, work)
