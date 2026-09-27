"""Bundle 6D-A review & approval routes (spec §16).

The data GET is read-only: it never records REVIEW_PRESENTED (only the
human-facing review page does, Task 14). Ownership failures are 404,
ReviewRefused is 409 with its exact reason, and bodies forbid extra keys.
No route here reaches any SUBMIT authority."""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from product.application_document_contract import ApplicationDocumentContractError
from product.autonomy_contract import Reach
from product.review_contract import derive_review_state
from product.docx_package import DocxPackageError
from webapp.api.dependencies import get_account_scope, get_conn
from webapp.persistence.autonomy_answers import AnswerValidationError
from webapp.services import review_answers, review_approval, review_documents
from webapp.services.document_blob_store import DocumentBlobError
from webapp.services.ownership import AccountScope
from webapp.services.pipeline import PipelineError
from webapp.services.review_application import ReviewRefused, review_snapshot, review_state

router = APIRouter(prefix="/api/workspaces/{workspace_id}/review", tags=["review"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApproveBody(_Body):
    displayed_binding_hash: str


class SelectBody(_Body):
    document_version_id: str
    expected_revision: int


class AnswerBody(_Body):
    answer_key: str
    value: Any
    reach: Reach


class AcceptBody(_Body):
    edited_value: Any = None
    reach: Reach


class DispositionBody(_Body):
    disposition: str


class AckBody(_Body):
    warning_key: str


class DeltaBody(_Body):
    kind: str
    answer_key: str | None = None
    subject: str | None = None
    required: bool
    question: str
    observed: dict[str, Any]
    source: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def call(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except ReviewRefused as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    except AnswerValidationError as exc:  # the answer itself is invalid for its subject
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (PipelineError, DocxPackageError, ApplicationDocumentContractError, DocumentBlobError) as exc:
        text = str(exc)
        raise HTTPException(status_code=404 if "not found" in text else 400, detail=text) from exc


def review_payload(conn, *, settings, account_id: str, workspace_id: str) -> dict[str, Any]:
    """Read only. The displayed Reviewable and the exposed binding hash come
    from ONE ReviewSnapshot, and every read happens inside one SQLite read
    transaction, so the payload is one coherent database snapshot: it can
    never show content A with approvable hash B. (The page's
    record_presented re-check then refuses A if state moved on since.)"""
    now = _now()
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")  # deferred: the snapshot is fixed at the first read below
    try:
        snapshot = review_snapshot(conn, settings=settings, account_id=account_id,
                                   application_workspace_id=workspace_id, now=now)
        reviewable, state = snapshot.reviewable, derive_review_state(snapshot)
        mode = review_approval.review_view_mode(conn, settings=settings, account_id=account_id,
                                                application_workspace_id=workspace_id, now=now)
        drafts = review_documents.newer_drafts(conn, account_id=account_id, application_workspace_id=workspace_id)
    finally:
        if owns_transaction:
            conn.rollback()  # end the read transaction; this path never writes
    return {"reviewable": jsonable(reviewable), "binding_hash": state.binding_hash, "newer_drafts": drafts,
            "state": {"state": state.state, "reasons": list(state.reasons), "blocking": list(state.blocking),
                      "binding_matches": state.binding_matches, "approval_effective": state.approval_effective},
            "view_mode": mode}


# The bare GET /api/workspaces/{id}/review is the pre-existing Phase 2 review
# surface (webapp/api/review.py); the 6D-A read-only state lives at /review/state.
@router.get("/state")
def get_review(workspace_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return call(lambda: review_payload(conn, settings=request.app.state.settings, account_id=scope.account_id,
                                       workspace_id=workspace_id))


@router.post("/documents/{kind}")
def post_replace(workspace_id: str, kind: str, request: Request, file: UploadFile = File(...),
                 expected_revision: int = Form(...), conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    content = file.file.read(10 * 1024 * 1024 + 1)
    if file.file.read(1):
        raise HTTPException(status_code=400, detail="DOCX exceeds the compressed-size limit")
    return call(lambda: review_documents.replace_document(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        kind=kind, filename=file.filename or "", content=content, expected_revision=expected_revision,
        actor=scope.account_id, now=_now()))


@router.post("/documents/{kind}/select")
def post_select(workspace_id: str, kind: str, body: SelectBody, request: Request,
                conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return call(lambda: review_documents.select_document(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        kind=kind, document_version_id=body.document_version_id, expected_revision=body.expected_revision,
        actor=scope.account_id, now=_now()))


@router.post("/save")
def post_save(workspace_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
              scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    def action():
        review_state(conn, settings=request.app.state.settings, account_id=scope.account_id,
                     application_workspace_id=workspace_id, now=_now())  # ownership -> 404
        return review_documents.save_changes(conn, settings=request.app.state.settings, account_id=scope.account_id,
                                             application_workspace_id=workspace_id, actor=scope.account_id,
                                             now=_now())
    return call(action)


@router.post("/answers")
def post_answer(workspace_id: str, body: AnswerBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return call(lambda: review_answers.answer_field(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        answer_key=body.answer_key, value=body.value, reach=body.reach, actor=scope.account_id, now=_now()))


@router.post("/proposals/{proposal_id}/accept")
def post_accept(workspace_id: str, proposal_id: str, body: AcceptBody, request: Request,
                conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return call(lambda: review_answers.accept_proposal(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        proposal_id=proposal_id, edited_value=body.edited_value, reach=body.reach, actor=scope.account_id,
        now=_now()))


@router.post("/fields/{answer_key}/disposition")
def post_disposition(workspace_id: str, answer_key: str, body: DispositionBody, request: Request,
                     conn: sqlite3.Connection = Depends(get_conn),
                     scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return call(lambda: review_answers.set_field_disposition(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        answer_key=answer_key, disposition=body.disposition, actor=scope.account_id, now=_now()))


@router.post("/warnings/ack")
def post_ack(workspace_id: str, body: AckBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
             scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return call(lambda: review_answers.acknowledge_warning(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        warning_key=body.warning_key, actor=scope.account_id, now=_now()))


@router.post("/approve")
def post_approve(workspace_id: str, body: ApproveBody, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return call(lambda: review_approval.approve(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        displayed_binding_hash=body.displayed_binding_hash, actor=scope.account_id, now=_now()))


@router.post("/revoke")
def post_revoke(workspace_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return call(lambda: review_approval.revoke(
        conn, settings=request.app.state.settings, account_id=scope.account_id, application_workspace_id=workspace_id,
        actor=scope.account_id, now=_now()))


@router.post("/deltas", status_code=201)
def post_delta(workspace_id: str, body: DeltaBody, conn: sqlite3.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    """Internal intake for 6D-B (same account scope)."""
    return call(lambda: review_approval.open_review_delta(
        conn, account_id=scope.account_id, application_workspace_id=workspace_id, kind=body.kind,
        answer_key=body.answer_key, subject=body.subject, required=body.required, question=body.question,
        observed=body.observed, source=body.source, now=_now()))


@router.get("/documents/{kind}/preview")
def get_preview(workspace_id: str, kind: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    """A read-only rendering of the currently selected document's exact bytes."""
    from webapp.persistence.application_documents import get_document_version, get_selection
    from webapp.services.docx_preview import docx_paragraphs
    from webapp.services.document_blob_store import DocumentBlobStore

    def action():
        review_state(conn, settings=request.app.state.settings, account_id=scope.account_id,
                     application_workspace_id=workspace_id, now=_now())  # ownership -> 404
        selection = get_selection(conn, workspace_id, kind, account_id=scope.account_id)
        document = get_document_version(conn, selection["document_version_id"], account_id=scope.account_id) \
            if selection else None
        if document is None:
            raise LookupError(kind)
        data = DocumentBlobStore(request.app.state.settings.documents_root).read(document)
        try:
            paragraphs = docx_paragraphs(data)
        except ValueError as exc:
            raise ReviewRefused("not_previewable") from exc
        return {"document_version_id": document["id"], "sha256": document["sha256"], "paragraphs": paragraphs}
    return call(action)
