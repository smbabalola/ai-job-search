"""Bundle 6D-A: document replace/select and Save changes (spec §7.2-7.4, §13).

Every DB mutation commits together with its review event; blob publication
may precede the transaction (an orphaned content-addressed blob on rollback
is harmless). Save changes is the user's own v2 Gate 4 of the exact current
selections, and records PACK_CONFIRMED inside that same transaction. Nothing
here is ever called by autonomy or the pipeline: selections move only by an
explicit user action."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from product.review_contract import ReviewWarning, WarningLevel, warning_key
from webapp.config import Settings
from webapp.persistence import review_approval as ra
from webapp.persistence.application_documents import get_document_version, get_selection
from webapp.persistence.artifacts import get_current_artifact
from webapp.services.application_documents import apply_selection, record_uploaded_version, store_upload_blob
from webapp.services.application_pack import confirm_application_pack
from webapp.services.autonomy_controls import run_immediate
from webapp.services.review_application import KINDS, ReviewRefused, review_state

_GENERATION = "application_document_generation"


def _current_generation_id(conn, ws: str) -> str | None:
    generation = get_current_artifact(conn, ws, _GENERATION)
    return generation["id"] if generation else None


def _select(conn, ws: str, *, kind: str, document_version_id: str, expected_revision: int,
            account_id: str) -> dict[str, Any]:
    try:
        return apply_selection(conn, ws, kind=kind, document_version_id=document_version_id,
                               expected_revision=expected_revision, account_id=account_id)
    except ValueError as exc:  # set_selection's revision check: a stale tab
        raise ReviewRefused("stale_selection") from exc


def replace_document(conn, *, settings: Settings, account_id: str, application_workspace_id: str, kind: str,
                     filename: str, content: bytes, expected_revision: int, actor: str,
                     now: datetime) -> dict[str, Any]:
    ws = application_workspace_id
    blob = store_upload_blob(kind=kind, filename=filename, content=content, documents_root=settings.documents_root)

    def work() -> dict[str, Any]:
        version = record_uploaded_version(conn, ws, kind=kind, filename=filename, blob=blob, account_id=account_id)
        selection = _select(conn, ws, kind=kind, document_version_id=version["id"],
                            expected_revision=expected_revision, account_id=account_id)
        ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="DOCUMENT_REPLACED",
                        binding_hash=None, detail={"kind": kind, "document_version_id": version["id"],
                                                   "based_on_generation_artifact_id": _current_generation_id(conn, ws)},
                        actor=actor, now=now)
        return {"document_version_id": version["id"], "revision": selection["revision"]}
    return run_immediate(conn, work)


def select_document(conn, *, settings: Settings, account_id: str, application_workspace_id: str, kind: str,
                    document_version_id: str, expected_revision: int, actor: str, now: datetime) -> dict[str, Any]:
    ws = application_workspace_id

    def work() -> dict[str, Any]:
        selection = _select(conn, ws, kind=kind, document_version_id=document_version_id,
                            expected_revision=expected_revision, account_id=account_id)
        ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="SELECTION_CHANGED",
                        binding_hash=None, detail={"kind": kind, "document_version_id": document_version_id,
                                                   "based_on_generation_artifact_id": _current_generation_id(conn, ws)},
                        actor=actor, now=now)
        return {"document_version_id": document_version_id, "revision": selection["revision"]}
    return run_immediate(conn, work)


def save_changes(conn, *, settings: Settings, account_id: str, application_workspace_id: str, actor: str,
                 now: datetime) -> dict[str, Any]:
    """The user's v2 confirmation of exactly the current selections. Works
    whether automation is paused or halted (approval is consent, not
    authority)."""
    ws = application_workspace_id
    revisions = {}
    for kind in KINDS:
        selection = get_selection(conn, ws, kind, account_id=account_id)
        if selection is None:
            raise ReviewRefused("no_selection")
        revisions[kind] = selection["revision"]
    recorded: dict[str, Any] = {}

    def on_confirmed(confirmed: dict[str, Any]) -> None:
        state = review_state(conn, settings=settings, account_id=account_id, application_workspace_id=ws, now=now)
        ra.record_event(conn, account_id=account_id, application_workspace_id=ws, event="PACK_CONFIRMED",
                        binding_hash=state.binding_hash,
                        detail={"pack_artifact_id": confirmed["artifact"]["id"]}, actor=actor, now=now)
        recorded.update(pack_artifact_id=confirmed["artifact"]["id"], binding_hash=state.binding_hash)

    confirm_application_pack(conn, ws, effective_date=now.date().isoformat(), documents_root=settings.documents_root,
                             account_id=account_id, document_selection_revisions=revisions,
                             on_confirmed=on_confirmed)
    return recorded


def _last_selection_basis(conn, ws: str, kind: str, document_version_id: str) -> str | None:
    """The generation current when the user put this document in place."""
    basis = None
    for e in ra.events(conn, ws):
        if e["event"] in ("DOCUMENT_REPLACED", "SELECTION_CHANGED") and e["detail"].get("kind") == kind \
                and e["detail"].get("document_version_id") == document_version_id:
            basis = e["detail"].get("based_on_generation_artifact_id")
    return basis


def newer_drafts(conn, *, account_id: str, application_workspace_id: str) -> dict[str, dict[str, str]]:
    """kind -> {selected_version_id, current_ai_version_id} for each kind where
    the current AI generation (its application_document_generation pointer)
    offers a version other than the selected one. Read only: the selection is
    never changed; the review page offers "Use the new draft" from this."""
    ws = application_workspace_id
    generation_id = _current_generation_id(conn, ws)
    if generation_id is None:
        return {}
    out = {}
    for kind in KINDS:
        selection = get_selection(conn, ws, kind, account_id=account_id)
        if selection is None:
            continue
        current_ai = conn.execute(
            "SELECT id FROM application_document_versions WHERE source_workspace_id = ? AND account_id = ? "
            "AND document_kind = ? AND source_generation_artifact_id = ?",
            (ws, account_id, kind, generation_id)).fetchone()
        if current_ai is None:
            continue
        selected = get_document_version(conn, selection["document_version_id"], account_id=account_id)
        if selected is None or selected["id"] == current_ai["id"]:
            continue
        if selected["origin"] == "ai_generated" or                 _last_selection_basis(conn, ws, kind, selected["id"]) != generation_id:
            out[kind] = {"selected_version_id": selected["id"], "current_ai_version_id": current_ai["id"]}
    return out


def newer_draft_warnings(conn, *, account_id: str, application_workspace_id: str) -> tuple[ReviewWarning, ...]:
    """ATTENTION when the current AI generation offers a version other than the
    selected one (never by rowid or time; the selection is never changed)."""
    return tuple(
        ReviewWarning(warning_key("newer_ai_draft", kind, draft), WarningLevel.ATTENTION,
                      f"A newer AI draft of your {kind.replace('_', ' ')} is available; your version is kept")
        for kind, draft in newer_drafts(conn, account_id=account_id,
                                        application_workspace_id=application_workspace_id).items())
