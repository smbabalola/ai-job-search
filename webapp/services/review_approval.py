"""Bundle 6D-A: the approval transaction, revocation, expiry and the
invalidation audit (spec §9.2-9.4, §13).

Approval is consent, not authority: it never checks pause or the kill
switch, and it never creates, confirms or changes what it approves. Its
write contract is exact: one application_approvals row, one APPROVED event,
one DELTA_RESOLVED per delta it resolves, and the 6C queue wake."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from product.review_contract import delta_resolved, invalidation_reasons
from webapp.config import Settings
from webapp.persistence import autonomy_prepare as ap
from webapp.persistence import review_approval as ra
from webapp.persistence.application_documents import get_document_version
from webapp.services.autonomy_controls import run_immediate
from webapp.services.review_application import ReviewRefused, review_state

_NO_PACK = ("save_document_changes", "exact_files_required")


def _media_types(conn, account_id: str, binding: dict[str, Any]) -> dict[str, str]:
    out = {}
    for d in binding["documents"]:
        row = get_document_version(conn, d["document_version_id"], account_id=account_id)
        if row is not None:
            out[d["kind"]] = row["media_type"]
    return out


def approve(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
            displayed_binding_hash: str, actor: str, now: datetime, batch_id: str | None = None) -> dict[str, Any]:
    ws = application_workspace_id

    def work() -> dict[str, Any]:
        state = review_state(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
        if state.state in ("CLOSED", "NOT_READY"):
            raise ReviewRefused("not_approvable")
        if state.binding_hash is None or any(b.split(":", 1)[0] in _NO_PACK for b in state.blocking):
            raise ReviewRefused("no_pack")
        if displayed_binding_hash != state.binding_hash:
            raise ReviewRefused("stale")
        if state.blocking:
            raise ReviewRefused("blocking")
        if "unacknowledged_attention" in state.reasons:
            raise ReviewRefused("unacknowledged_attention")
        if state.approval_effective:
            raise ReviewRefused("already_approved")
        latest = ra.latest_approval(conn, ws)
        media = _media_types(conn, account_id, state.binding)
        resolved = [d["id"] for d in ra.open_deltas(conn, ws)
                    if delta_resolved(d, state.binding, document_media_types=media)]
        approval = ra.insert_approval(conn, account_id=account_id, application_workspace_id=ws,
                                      binding=state.binding, binding_hash=state.binding_hash,
                                      supersedes_id=latest["id"] if latest else None, batch_id=batch_id,
                                      resolved_delta_ids=resolved, actor=actor, now=now)
        ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="APPROVED",
                        binding_hash=state.binding_hash, detail={"approval_id": approval["id"], "batch_id": batch_id},
                        actor=actor, now=now)
        for delta_id in resolved:
            ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="DELTA_RESOLVED",
                            binding_hash=state.binding_hash, detail={"delta_id": delta_id, "approval_id": approval["id"]},
                            actor=actor, now=now)
        ap.wake(conn, queue="APPLICATION", item_id=ws, now=now)  # wake-only; no-op when not enrolled
        return {"approval_id": approval["id"], "binding_hash": state.binding_hash, "resolved_delta_ids": resolved}
    return run_immediate(conn, work)


def revoke(conn, *, settings: Settings, account_id: str, application_workspace_id: str, actor: str,
           now: datetime) -> dict[str, Any]:
    ws = application_workspace_id

    def work() -> dict[str, Any]:
        review_state(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)  # ownership
        latest = ra.latest_approval(conn, ws)
        if latest is None:
            raise ReviewRefused("no_approval")
        if ra.approval_revoked(conn, latest):
            raise ReviewRefused("already_revoked")
        ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="REVOKED",
                        binding_hash=latest["binding_hash"], detail={"approval_id": latest["id"]}, actor=actor, now=now)
        return {"approval_id": latest["id"]}
    return run_immediate(conn, work)


def current_invalidation_reasons(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                                 now: datetime) -> list[str]:
    """The exact reason set for the latest approval (empty when effective or
    none): state reasons, with binding_changed expanded by component names."""
    ws = application_workspace_id
    latest = ra.latest_approval(conn, ws)
    if latest is None:
        return []
    state = review_state(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
    if state.approval_effective:
        return []
    reasons = set(state.reasons)
    if "binding_changed" in reasons and state.binding is not None:
        reasons |= set(invalidation_reasons(latest["binding"], state.binding))
    return sorted(reasons)


def record_invalidation_if_needed(conn, *, settings: Settings, account_id: str, application_workspace_id: str,
                                  now: datetime) -> bool:
    """spec §13: once per approval per exact reason set (hashes are payload
    only). Also EXPIRED once per approval when its TTL has passed."""
    ws = application_workspace_id

    def work() -> bool:
        reasons = current_invalidation_reasons(conn, settings=settings, account_id=account_id,
                                               application_workspace_id=ws, now=now)
        if not reasons:
            return False
        latest = ra.latest_approval(conn, ws)
        wrote = False
        if not ra.invalidation_recorded(conn, ws, latest["id"], reasons):
            state = review_state(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
            ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="APPROVAL_INVALIDATED",
                            binding_hash=state.provisional_hash,
                            detail={"approval_id": latest["id"], "reasons": reasons,
                                    "previous_hash": latest["binding_hash"], "current_hash": state.provisional_hash},
                            actor="system", now=now)
            wrote = True
        if "expired" in reasons and not any(e["event"] == "EXPIRED" and e["detail"].get("approval_id") == latest["id"]
                                            for e in ra.events(conn, ws)):
            ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="EXPIRED",
                            binding_hash=latest["binding_hash"], detail={"approval_id": latest["id"]},
                            actor="system", now=now)
        return wrote
    return run_immediate(conn, work)


def reconcile_approvals(conn, *, settings: Settings, now: datetime) -> int:
    """Reduce-only 6C sweep: record invalidations for every approved
    application, each in its own short transaction."""
    written = 0
    rows = conn.execute("SELECT DISTINCT application_workspace_id, account_id FROM application_approvals").fetchall()
    for row in rows:
        try:
            written += record_invalidation_if_needed(conn, settings=settings, account_id=row["account_id"],
                                                     application_workspace_id=row["application_workspace_id"], now=now)
        except LookupError:
            continue
    return written
