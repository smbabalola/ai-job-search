from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

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
    PairingSecretInvalid,
    SessionScope,
    confirm_handoff_submission,
    discover_resumable_handoff_sessions,
    exchange_pairing_secret_for_credential,
    fetch_session_document,
    generate_pairing_secret,
    mint_session_token,
    project_session_snapshot,
    record_handoff_event,
    replay_handoff_session,
    resolve_account_scope_from_extension_credential,
    resolve_session_scope,
    resume_handoff_session,
    start_handoff_session,
)
from webapp.services.ownership import AccountScope, OwnedResourceNotFound

router = APIRouter(prefix="/api/handoff", tags=["handoff"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExchangePairingBody(StrictBody):
    one_time_secret: str


class StartSessionBody(StrictBody):
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


class SnapshotProjectionBody(StrictBody):
    # Normalized field types only — never an arbitrary candidate JSON
    # path. The server's closed mapping (project_session_snapshot) is
    # what decides which paths this list can ever select from; this
    # field is purely a filter over that mapping (design spec Section
    # 7.1).
    normalized_field_types: list[str]


def get_extension_scope(
    request: Request,
    x_handoff_credential: str = Header(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> AccountScope:
    try:
        return resolve_account_scope_from_extension_credential(
            conn, presented_secret=x_handoff_credential,
            base_profile_root=request.app.state.settings.profile_root,
        )
    except PairingSecretInvalid as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def get_session_scope(
    x_handoff_session_token: str = Header(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> SessionScope:
    try:
        return resolve_session_scope(conn, raw_token=x_handoff_session_token)
    except (HandoffSessionTokenInvalid, HandoffSessionExpired) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, (HandoffSessionNotFound, OwnedResourceNotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, HandoffSessionExpired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(
        exc,
        (HandoffPackNotFound, HandoffSessionNotActive, HandoffEventRejected,
         HandoffPackArtifactInvalid, HandoffDocumentKindUnsupported, HandoffPackStale),
    ):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/pairing/generate", status_code=201)
def post_generate_pairing(
    scope: AccountScope = Depends(get_account_scope),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {
        "one_time_secret": generate_pairing_secret(conn, account_id=scope.account_id),
        "account_id": scope.account_id,
    }


@router.post("/pairing/exchange", status_code=201)
def post_exchange_pairing(
    body: ExchangePairingBody,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        return exchange_pairing_secret_for_credential(
            conn, one_time_secret=body.one_time_secret,
        )
    except HandoffError as exc:
        raise _translate(exc) from exc


@router.post("/sessions", status_code=201)
def post_start_session(
    body: StartSessionBody,
    scope: AccountScope = Depends(get_extension_scope),
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        session = start_handoff_session(conn, scope, **body.model_dump())
    except (HandoffError, OwnedResourceNotFound) as exc:
        raise _translate(exc) from exc
    token = mint_session_token(conn, handoff_session_id=session["id"])
    return {**session, "session_token": token}


@router.get("/sessions/discover")
def get_discover_sessions(
    workspace_id: str, target_domain: str,
    scope: AccountScope = Depends(get_extension_scope),
    conn: sqlite3.Connection = Depends(get_conn),
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


@router.post("/sessions/{session_id}/resume", status_code=201)
def post_resume_session(
    session_id: str,
    scope: AccountScope = Depends(get_extension_scope),
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        token = resume_handoff_session(conn, scope, handoff_session_id=session_id)
    except HandoffError as exc:
        raise _translate(exc) from exc
    return {"session_token": token}


@router.post("/sessions/{session_id}/snapshot")
def post_session_snapshot(
    session_id: str, body: SnapshotProjectionBody,
    scope: SessionScope = Depends(get_session_scope),
    conn: sqlite3.Connection = Depends(get_conn),
):
    # workspace_id/pack_artifact_id are never accepted as request
    # parameters — both come from the resolved SessionScope, which is
    # itself resolved entirely from the presented session token. A token
    # for session A can never be used to fetch session B's projection,
    # even for the same account (design spec Section 7.1).
    if scope.handoff_session_id != session_id:
        raise _translate(HandoffSessionNotFound(f"handoff session {session_id!r} not found"))
    try:
        snapshot = project_session_snapshot(
            conn, scope, normalized_field_types=body.normalized_field_types,
        )
    except HandoffError as exc:
        raise _translate(exc) from exc
    return {"snapshot": snapshot}


@router.get("/sessions/{session_id}/documents/{kind}")
def get_session_document(
    session_id: str, kind: str,
    scope: SessionScope = Depends(get_session_scope),
    conn: sqlite3.Connection = Depends(get_conn),
    documents_root: Path = Depends(get_documents_root),
):
    # No workspace_id/pack_artifact_id request parameter exists on this
    # route at all — both are derived entirely from the resolved
    # SessionScope (itself resolved entirely from the presented session
    # token), exactly like post_session_snapshot above. A token for
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
                f'filename="{kind}.docx"'
            ),
            "X-Content-Hash": rendered_file.content_hash,
        },
    )


@router.post("/sessions/{session_id}/events", status_code=201)
def post_record_event(
    session_id: str, body: RecordEventBody,
    scope: SessionScope = Depends(get_session_scope),
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        return record_handoff_event(
            conn, scope, handoff_session_id=session_id, **body.model_dump()
        )
    except HandoffError as exc:
        raise _translate(exc) from exc


@router.get("/sessions/{session_id}/events")
def get_replay_events(
    session_id: str,
    scope: SessionScope = Depends(get_session_scope),
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        return replay_handoff_session(conn, scope, session_id)
    except HandoffError as exc:
        raise _translate(exc) from exc


@router.post("/sessions/{session_id}/confirm-submission", status_code=201)
def post_confirm_submission(
    session_id: str, body: ConfirmSubmissionBody,
    scope: SessionScope = Depends(get_session_scope),
    conn: sqlite3.Connection = Depends(get_conn),
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
