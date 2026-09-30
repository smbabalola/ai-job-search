from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from webapp.api.route_classes import EXTENSION, PUBLIC, USER
from webapp.api.dependencies import (
    get_account_scope,
    get_conn,
    get_documents_root,
    get_extensions_dir,
)
from webapp.services.handoff import (
    HandoffDocumentKindUnsupported,
    HandoffError,
    HandoffEventRejected,
    HandoffPackArtifactInvalid,
    HandoffPackNotFound,
    HandoffPackStale,
    HandoffSessionExpired,
    HandoffSessionNotActive,
    HandoffSessionNotFound,
    HandoffSessionTokenInvalid,
    SessionScope,
    confirm_handoff_submission,
    discover_resumable_handoff_sessions,
    fetch_session_document,
    mint_session_token,
    record_handoff_event,
    replay_handoff_session,
    resolve_session_scope,
    resume_handoff_session,
    start_handoff_session,
)
from webapp.api.extension_auth import ExtensionScope, get_extension_scope
from webapp.services.extension_auth import AccountMismatch, ExtensionPrincipal, TicketInvalid, consume_handoff_ticket
from webapp.services.ownership import AccountScope, OwnedResourceNotFound
from webapp.persistence import dbapi

router = APIRouter(prefix="/api/handoff", tags=["handoff"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartSessionBody(StrictBody):
    handoff_ticket: str
    workspace_id: str
    pack_artifact_id: str
    target_url: str
    target_domain: str
    ats_adapter_id: str
    ats_adapter_version: str


class RecordEventBody(StrictBody):
    event_id: str
    event_type: str
    event_payload: dict
    normalized_field_type: str | None = None
    page_field_key: str | None = None
    observed_at: str | None = None


class ConfirmSubmissionBody(StrictBody):
    mark_workflow_applied: bool = False
    effective_date: str | None = None


def get_session_scope(
    x_handoff_session_token: str = Header(...),
    device: ExtensionScope = Depends(get_extension_scope),
    conn: dbapi.Connection = Depends(get_conn),
) -> SessionScope:
    """A handoff session token is honoured only with a bearer device token of
    the same account (spec §9.2): a leaked session token is useless alone."""
    try:
        scope = resolve_session_scope(conn, raw_token=x_handoff_session_token)
    except (HandoffSessionTokenInvalid, HandoffSessionExpired) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if scope.account_id != device.account_id:
        raise HTTPException(status_code=404, detail="handoff session not found")
    return scope


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, (HandoffSessionNotFound, OwnedResourceNotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, HandoffSessionExpired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(
        exc,
        (
            HandoffPackNotFound,
            HandoffSessionNotActive,
            HandoffEventRejected,
            HandoffPackStale,
            HandoffPackArtifactInvalid,
            HandoffDocumentKindUnsupported,
        ),
    ):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/sessions", status_code=201, dependencies=[Depends(EXTENSION)])
def post_start_session(
    body: StartSessionBody,
    request: Request,
    scope: ExtensionScope = Depends(get_extension_scope),
    conn: dbapi.Connection = Depends(get_conn),
):
    # X4: the web page's ticket must belong to the same user as this device.
    try:
        consume_handoff_ticket(conn, body.handoff_ticket, principal=ExtensionPrincipal(
            scope.device_id, scope.user_id, scope.account_id), workspace_id=body.workspace_id, purpose="HANDOFF",
            now=datetime.now(timezone.utc), secret=request.app.state.settings.secret_key)
    except (AccountMismatch, TicketInvalid) as exc:
        conn.rollback()
        raise HTTPException(status_code=403, detail={"error": exc.code, "message": str(exc)}) from exc
    try:
        session = start_handoff_session(conn, scope, **body.model_dump(exclude={"handoff_ticket"}))
    except (HandoffError, OwnedResourceNotFound) as exc:
        raise _translate(exc) from exc
    token = mint_session_token(conn, handoff_session_id=session["id"])
    return {**session, "session_token": token}


@router.get("/sessions/discover", dependencies=[Depends(EXTENSION)])
def get_discover_sessions(
    workspace_id: str, target_domain: str,
    scope: AccountScope = Depends(get_extension_scope),
    conn: dbapi.Connection = Depends(get_conn),
):
    # Metadata-only: never mints or rotates a token, and never refreshes
    # last_activity_at for any session it lists — merely listing resumable
    # candidates is not activity. Minting/rotation happens only in
    # post_resume_session, for the one session the caller actually chose
    # (design spec Section 3.2).
    try:
        sessions = discover_resumable_handoff_sessions(
            conn, scope, workspace_id=workspace_id, target_domain=target_domain,
        )
        return {"sessions": sessions}
    except OwnedResourceNotFound as exc:
        raise _translate(exc) from exc


@router.post("/sessions/{session_id}/resume", status_code=201, dependencies=[Depends(EXTENSION)])
def post_resume_session(
    session_id: str,
    scope: AccountScope = Depends(get_extension_scope),
    conn: dbapi.Connection = Depends(get_conn),
):
    try:
        token = resume_handoff_session(conn, scope, handoff_session_id=session_id)
    except HandoffError as exc:
        raise _translate(exc) from exc
    return {"session_token": token}


def download_filename(kind: str, media_type: str) -> str:
    """The ASCII fallback filename: the kind plus the document's own extension (DOCX or PDF)."""
    from product.application_document_contract import MEDIA_TYPE_EXTENSIONS
    return f"{kind}{MEDIA_TYPE_EXTENSIONS.get(media_type, '.docx')}"


@router.get("/sessions/{session_id}/documents/{kind}", dependencies=[Depends(EXTENSION)])
def get_session_document(
    session_id: str, kind: str,
    scope: SessionScope = Depends(get_session_scope),
    conn: dbapi.Connection = Depends(get_conn),
    documents_root: Path = Depends(get_documents_root),
):
    # No workspace_id/pack_artifact_id request parameter exists on this
    # route at all — both are derived entirely from the resolved
    # SessionScope (itself resolved entirely from the presented session
    # token), exactly like every session-scoped route here. A token for
    # session A can never be used to fetch session B's document, and the
    # session's pinned pack_artifact_id is always used explicitly, so a
    # newer Application Pack confirmed for the same workspace after this
    # session started is never silently substituted in (design spec
    # Section 7).
    if scope.handoff_session_id != session_id:
        raise _translate(HandoffSessionNotFound(f"handoff session {session_id!r} not found"))
    try:
        rendered_file = fetch_session_document(
            conn, scope, kind=kind, documents_root=documents_root,
        )
    except HandoffError as exc:
        raise _translate(exc) from exc
    return Response(
        content=rendered_file.content,
        media_type=rendered_file.mime_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(rendered_file.filename)}; "
                f'filename="{download_filename(kind, rendered_file.mime_type)}"'
            ),
            "X-Content-Hash": rendered_file.content_hash,
        },
    )


@router.post("/sessions/{session_id}/events", status_code=201, dependencies=[Depends(EXTENSION)])
def post_record_event(
    session_id: str, body: RecordEventBody,
    scope: SessionScope = Depends(get_session_scope),
    conn: dbapi.Connection = Depends(get_conn),
):
    try:
        return record_handoff_event(
            conn, scope, handoff_session_id=session_id, **body.model_dump()
        )
    except HandoffError as exc:
        raise _translate(exc) from exc


@router.get("/sessions/{session_id}/events", dependencies=[Depends(EXTENSION)])
def get_replay_events(
    session_id: str,
    scope: SessionScope = Depends(get_session_scope),
    conn: dbapi.Connection = Depends(get_conn),
):
    try:
        return replay_handoff_session(conn, scope, session_id)
    except HandoffError as exc:
        raise _translate(exc) from exc


@router.post("/sessions/{session_id}/confirm-submission", status_code=201, dependencies=[Depends(EXTENSION)])
def post_confirm_submission(
    session_id: str, body: ConfirmSubmissionBody,
    scope: SessionScope = Depends(get_session_scope),
    conn: dbapi.Connection = Depends(get_conn),
    extensions_dir: Path = Depends(get_extensions_dir),
):
    try:
        return confirm_handoff_submission(
            conn, scope, handoff_session_id=session_id,
            mark_workflow_applied=body.mark_workflow_applied,
            effective_date=body.effective_date,
            extensions_dir=extensions_dir,
        )
    except HandoffError as exc:
        raise _translate(exc) from exc
