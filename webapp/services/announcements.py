"""Announcements (Bundle 7 spec §18, §19.2): staff write Markdown, users see
sanitized HTML. Published announcements appear in the Inbox of the accounts
in their audience (``ALL`` or one plan) and, when staff choose, by email.

The renderer escapes every character of the input first and then turns a
small Markdown subset back into tags it writes itself: paragraphs, line
breaks, ``- `` lists, ``**bold**``, ``*em*`` and ``[text](url)`` links whose
scheme is http, https or mailto. No raw HTML survives, so ``<script>`` is
text and ``javascript:`` links are dropped (their text stays)."""
from __future__ import annotations

import html
import re
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from webapp.persistence import dbapi

__all__ = ["AUDIENCES", "SEVERITIES", "active_for_account", "audience_accounts", "create", "publish",
           "render_markdown", "withdraw"]

AUDIENCES = ("ALL", "PLAN:free", "PLAN:pro", "PLAN:power")
SEVERITIES = ("INFO", "WARNING", "CRITICAL")
SAFE_SCHEMES = ("http://", "https://", "mailto:")

_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)")  # a URL may hold one level of (…)
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_EM = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")


def _link(match: re.Match) -> str:
    text, url = match.group(1), html.unescape(match.group(2))
    if not url.lower().startswith(SAFE_SCHEMES):
        return text
    return f'<a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{text}</a>'


def _inline(escaped: str) -> str:
    return _EM.sub(r"<em>\1</em>", _BOLD.sub(r"<strong>\1</strong>", _LINK.sub(_link, escaped)))


def render_markdown(source: str) -> str:
    blocks = re.split(r"\n\s*\n", html.escape(source or "", quote=True).replace("\r\n", "\n").strip())
    out = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if all(line.startswith("- ") for line in lines):
            out.append("<ul>" + "".join(f"<li>{_inline(line[2:])}</li>" for line in lines) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(_inline(line) for line in lines) + "</p>")
    return "\n".join(out)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def create(conn: dbapi.Connection, *, title: str, body_markdown: str, audience: str, severity: str,
           expires_at: datetime | None, created_by: str | None, now: datetime) -> str:
    """No commit. A draft (unpublished)."""
    if audience not in AUDIENCES:
        raise ValueError(f"audience must be one of {', '.join(AUDIENCES)}")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    if not title.strip() or not body_markdown.strip():
        raise ValueError("an announcement needs a title and a body")
    announcement_id = f"ann_{uuid.uuid4().hex[:20]}"
    conn.execute("INSERT INTO announcements (id, title, body_markdown, audience, severity, published_at, expires_at, "
                 "created_by, created_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                 (announcement_id, title.strip(), body_markdown, audience, severity,
                  None if expires_at is None else _iso(expires_at), created_by, _iso(now)))
    return announcement_id


def _plan_of(conn: dbapi.Connection, settings: Any, account_id: str, now: datetime) -> str:
    from webapp.services.entitlements import gate_for
    return gate_for(settings).entitlements(conn, SimpleNamespace(account_id=account_id), now=now).plan_id


def audience_accounts(conn: dbapi.Connection, audience: str, *, settings: Any, now: datetime) -> list[str]:
    """ACTIVE customer accounts in the audience."""
    ids = [r[0] for r in conn.execute("SELECT id FROM accounts WHERE kind = 'candidate' AND status = 'ACTIVE' "
                                      "ORDER BY id").fetchall()]
    if audience == "ALL":
        return ids
    plan = audience.split(":", 1)[1]
    return [account_id for account_id in ids if _plan_of(conn, settings, account_id, now) == plan]


def publish(conn: dbapi.Connection, announcement_id: str, *, email: bool, settings: Any, now: datetime) -> int:
    """No commit. Marks it published and notifies its audience (in-app; and by
    email when ``email``). Returns the number of accounts reached."""
    from webapp import comms
    from webapp.services.notifications import notify
    row = conn.execute("SELECT * FROM announcements WHERE id = ?", (announcement_id,)).fetchone()
    if row is None or row["withdrawn_at"] is not None:
        raise ValueError("unknown or withdrawn announcement")
    if row["published_at"] is None:
        conn.execute("UPDATE announcements SET published_at = ? WHERE id = ?", (_iso(now), announcement_id))
    reached = 0
    for account_id in audience_accounts(conn, row["audience"], settings=settings, now=now):
        if notify(conn, account_id=account_id, kind="announcement.published", subject_type="announcement",
                  subject_id=announcement_id, dedupe_key=f"announcement:{announcement_id}",
                  detail={"title": row["title"], "severity": row["severity"]}, now=now):
            reached += 1
        if email:
            owner = conn.execute("SELECT u.email_display FROM account_memberships m JOIN users u ON u.id = m.user_id "
                                 "WHERE m.account_id = ? AND m.role = 'OWNER' AND m.revoked_at IS NULL",
                                 (account_id,)).fetchone()
            if owner is not None:
                comms.enqueue(conn, category="PRODUCT", template_id="notify.immediate", to_address=owner[0],
                              payload={"title": row["title"], "body": row["body_markdown"],
                                       "action_url": f"{settings.app_origin.rstrip('/')}/inbox"},
                              account_id=account_id, idempotency_key=f"announcement:{announcement_id}:{account_id}",
                              now=now)
    return reached


def withdraw(conn: dbapi.Connection, announcement_id: str, *, now: datetime) -> None:
    """No commit."""
    conn.execute("UPDATE announcements SET withdrawn_at = COALESCE(withdrawn_at, ?) WHERE id = ?",
                 (_iso(now), announcement_id))


def active_for_account(conn: dbapi.Connection, account_id: str, *, settings: Any, now: datetime) -> list[dict]:
    """Published, unexpired, not withdrawn, and in this account's audience; rendered."""
    plan = _plan_of(conn, settings, account_id, now)
    rows = conn.execute(
        "SELECT id, title, body_markdown, severity, audience, published_at FROM announcements "
        "WHERE published_at IS NOT NULL AND withdrawn_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
        "AND audience IN ('ALL', ?) ORDER BY published_at DESC", (_iso(now), f"PLAN:{plan}")).fetchall()
    return [{"id": r["id"], "title": r["title"], "severity": r["severity"], "html": render_markdown(r["body_markdown"])}
            for r in rows]
