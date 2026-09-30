"""The derived Action-required inbox and the header badge (Bundle 7 spec §17.4).

Action required is computed live from the source state, so an item
disappears the moment its source resolves. Sources: open blockers, packs
awaiting review, open review deltas, fills awaiting submit, ambiguous
submissions, challenge handoffs in progress, and the 6C questions (the 6C
inbox summary folded in). Later tasks register more sources in ``SOURCES``
(rule acknowledgements, onboarding prerequisites).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from webapp.persistence import dbapi
from webapp.services.human_submit import submission_status
from webapp.services.review_application import review_state

logger = logging.getLogger("webapp.inbox")

MAX_WORKSPACES = 200


@dataclass(frozen=True)
class ActionItem:
    kind: str
    title_key: str
    href: str
    subject_type: str
    subject_id: str
    created_at: str | None = None


TITLES = {
    "blocker": "Answer a question for this application",
    "review": "Review the prepared application",
    "review_delta": "Review what changed in this application",
    "fill_awaiting_submit": "Submit the filled application",
    "submission_unclear": "Tell us whether this application went through",
    "challenge_handoff": "Complete the verification on the employer's page",
    "automation_question": "Automation needs your answer",
}


def _blockers(conn: dbapi.Connection, account_id: str) -> list[ActionItem]:
    rows = conn.execute("SELECT b.id, b.workspace_id, b.created_at FROM application_blockers b "
                        "JOIN workspaces w ON w.id = b.workspace_id WHERE w.account_id = ? AND b.status = 'open' "
                        "ORDER BY b.created_at, b.id", (account_id,)).fetchall()
    return [ActionItem("blocker", "blocker", f"/workspaces/{r['workspace_id']}", "workspace", r["workspace_id"],
                       r["created_at"]) for r in rows]


def _workspace_items(conn: dbapi.Connection, account_id: str, *, settings: Any, now: datetime) -> list[ActionItem]:
    rows = conn.execute("SELECT id FROM workspaces WHERE account_id = ? AND kind = 'job' "
                        "AND (workflow_status IS NULL OR workflow_status = 'drafted') "  # review_contract's open set
                        "ORDER BY updated_at DESC LIMIT ?", (account_id, MAX_WORKSPACES)).fetchall()
    items: list[ActionItem] = []
    for row in rows:
        ws = row["id"]
        try:
            review = review_state(conn, settings=settings, account_id=account_id, application_workspace_id=ws,
                                  now=now)
            if review.state == "READY_FOR_REVIEW" or (review.state == "NEEDS_REVIEW"
                                                       and "open_deltas" not in review.reasons):
                items.append(ActionItem("review", "review", f"/workspaces/{ws}/review", "workspace", ws))
            elif review.state == "NEEDS_REVIEW":
                items.append(ActionItem("review_delta", "review_delta", f"/workspaces/{ws}/review", "workspace", ws))
            status = submission_status(conn, settings=settings, account_id=account_id,
                                       application_workspace_id=ws, now=now)["status"]
        except Exception:  # noqa: BLE001 - one unreadable workspace never hides the rest
            logger.exception("inbox_workspace_skipped workspace=%s", ws)
            continue
        kind = {"SUBMIT_READY": "fill_awaiting_submit", "SUBMISSION_UNCLEAR": "submission_unclear",
                "CHALLENGE_WAITING": "challenge_handoff"}.get(status)
        if kind:
            items.append(ActionItem(kind, kind, f"/workspaces/{ws}/submit", "workspace", ws))
    return items


def _automation_questions(conn: dbapi.Connection, account_id: str) -> list[ActionItem]:
    from webapp.persistence import autonomy_prepare as ap
    from webapp.services.autonomy_inbox import ACTIONABLE
    return [ActionItem("automation_question", "automation_question", "/autonomy/inbox", n["subject_type"],
                       n["subject_id"], n.get("created_at"))
            for n in ap.open_notifications(conn, account_id) if n["kind"] in ACTIONABLE]


# Extra sources registered by later tasks: fn(conn, scope, settings=, now=) -> list[ActionItem].
SOURCES: list[Callable[..., list[ActionItem]]] = []


def action_required(conn: dbapi.Connection, scope: Any, *, settings: Any, now: datetime) -> list[ActionItem]:
    account_id = scope.account_id
    items = _blockers(conn, account_id) + _workspace_items(conn, account_id, settings=settings, now=now)
    items += _automation_questions(conn, account_id)
    for source in SOURCES:
        items += source(conn, scope, settings=settings, now=now)
    return items


def header_badge(conn: dbapi.Connection, scope: Any, *, settings: Any, now: datetime) -> int:
    from webapp.services.notifications import unread_critical
    return len(action_required(conn, scope, settings=settings, now=now)) + unread_critical(conn, scope.account_id)


def as_dict(item: ActionItem) -> dict[str, Any]:
    return {"kind": item.kind, "title": TITLES.get(item.title_key, item.title_key), "href": item.href,
            "subject_type": item.subject_type, "subject_id": item.subject_id, "created_at": item.created_at}
