"""The CV library routes and pages (Bundle 7 spec §14.6). A new CV checks the
library.cv_items gauge; every upload checks the storage.bytes gauge before a
blob is written; refused files answer DOCUMENT_REJECTED with their code."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from webapp.api.dependencies import get_account_scope, get_conn, get_documents_root
from webapp.api.route_classes import USER
from webapp.persistence import dbapi
from webapp.services import cv_library as lib
from webapp.services.ownership import AccountScope

router = APIRouter(dependencies=[Depends(USER)])

_PUBLIC_VERSION_FIELDS = ("id", "item_id", "version_no", "origin", "note", "library_visible", "media_type",
                          "original_filename", "byte_length", "sha256", "created_at", "parent_version_id",
                          "template_id", "document_version_id")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read(file: UploadFile) -> bytes:
    content = file.file.read(lib.MAX_UPLOAD_BYTES + 1)
    if len(content) > lib.MAX_UPLOAD_BYTES or file.file.read(1):
        raise lib.DocumentRejected("TOO_LARGE")
    return content


def _public(version: dict[str, Any]) -> dict[str, Any]:
    return {k: version[k] for k in _PUBLIC_VERSION_FIELDS if k in version}


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404 if "not found" in str(exc) else 409, detail=str(exc))


@router.get("/api/cvs")
def get_cvs(conn: dbapi.Connection = Depends(get_conn),
            scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return {"items": lib.list_items(conn, account_id=scope.account_id)}


@router.post("/api/cvs", status_code=201)
def post_cv(request: Request, title: str = Form(...), note: str = Form(""), description: str = Form(""),
            file: UploadFile = File(...), conn: dbapi.Connection = Depends(get_conn),
            documents_root: Path = Depends(get_documents_root),
            scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    content = _read(file)
    metering = request.app.state.metering
    metering.gauge_check(conn, scope, "library.cv_items", adding=1)
    metering.gauge_check(conn, scope, "storage.bytes", adding=len(content))
    lib.validate_upload(content, file.filename or "")  # refuse before the item exists
    now = _now()
    try:
        item = lib.create_item(conn, scope, title=title, description=description, now=now)
        version = lib.add_version(conn, scope, item_id=item["id"], content=content, filename=file.filename or "",
                                  media_type_hint=file.content_type, note=note, documents_root=documents_root,
                                  now=now)
    except lib.LibraryError as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()
    return {"item": item, "version": _public(version)}


@router.post("/api/cvs/{item_id}/versions", status_code=201)
def post_version(item_id: str, request: Request, note: str = Form(""), file: UploadFile = File(...),
                 conn: dbapi.Connection = Depends(get_conn), documents_root: Path = Depends(get_documents_root),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    if lib.get_item(conn, account_id=scope.account_id, item_id=item_id) is None:
        raise HTTPException(status_code=404, detail="CV not found")
    content = _read(file)
    request.app.state.metering.gauge_check(conn, scope, "storage.bytes", adding=len(content))
    try:
        version = lib.add_version(conn, scope, item_id=item_id, content=content, filename=file.filename or "",
                                  media_type_hint=file.content_type, note=note, documents_root=documents_root,
                                  now=_now())
    except lib.LibraryError as exc:
        conn.rollback()
        raise _not_found(exc) from exc
    conn.commit()
    return {"version": _public(version)}


def _state_change(conn: dbapi.Connection, scope: AccountScope, item_id: str, action) -> dict[str, Any]:
    try:
        action(conn, scope, item_id=item_id, now=_now())
    except lib.LibraryError as exc:
        raise _not_found(exc) from exc
    conn.commit()
    return {"item": lib.get_item(conn, account_id=scope.account_id, item_id=item_id)}


@router.post("/api/cvs/{item_id}/archive")
def post_archive(item_id: str, conn: dbapi.Connection = Depends(get_conn),
                 scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return _state_change(conn, scope, item_id, lib.archive_item)


@router.post("/api/cvs/{item_id}/unarchive")
def post_unarchive(item_id: str, conn: dbapi.Connection = Depends(get_conn),
                   scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    return _state_change(conn, scope, item_id, lib.unarchive_item)


@router.get("/api/cvs/{item_id}/versions/{version_id}/download")
def get_download(item_id: str, version_id: str, conn: dbapi.Connection = Depends(get_conn),
                 documents_root: Path = Depends(get_documents_root),
                 scope: AccountScope = Depends(get_account_scope)):
    try:
        version, content = lib.read_version(conn, account_id=scope.account_id, version_id=version_id,
                                            documents_root=documents_root)
    except lib.LibraryError as exc:
        raise HTTPException(status_code=404, detail="CV version not found") from exc
    if version["item_id"] != item_id:
        raise HTTPException(status_code=404, detail="CV version not found")
    from webapp.api.handoff import download_filename
    fallback = download_filename("cv", version["media_type"])
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(version['original_filename'])}"
    return Response(content=content, media_type=version["media_type"],
                    headers={"Content-Disposition": disposition, "X-Content-Hash": "sha256:" + version["sha256"]})


@router.get("/cvs")
def cvs_page(request: Request, conn: dbapi.Connection = Depends(get_conn),
             scope: AccountScope = Depends(get_account_scope)):
    return request.app.state.templates.TemplateResponse(request, "cvs/index.html", {
        "items": lib.list_items(conn, account_id=scope.account_id), "max_mb": lib.MAX_UPLOAD_BYTES // (1024 * 1024)})


@router.get("/cvs/{item_id}")
def cv_page(item_id: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
            scope: AccountScope = Depends(get_account_scope)):
    item = lib.get_item(conn, account_id=scope.account_id, item_id=item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="CV not found")
    versions = lib.list_versions(conn, account_id=scope.account_id, item_id=item_id)
    for version in versions:
        version["used_by"] = lib.usage_count(conn, account_id=scope.account_id, version_id=version["id"])
    return request.app.state.templates.TemplateResponse(request, "cvs/item.html", {
        "item": item, "versions": versions, "max_mb": lib.MAX_UPLOAD_BYTES // (1024 * 1024)})
