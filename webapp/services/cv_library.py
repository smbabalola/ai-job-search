"""The CV library (Bundle 7 spec §14): named CVs with immutable, numbered
versions. Every upload is validated before anything is stored (Review Focus
4): no blob, no document version and no library version for a refused file.
Library documents live under the account's profile workspace; a version is
selectable for any of the account's applications and is used byte-exact.
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any

from product.application_document_contract import (
    DOCX_MEDIA_TYPE, MEDIA_TYPE_EXTENSIONS, PDF_MEDIA_TYPE, validate_display_filename,
)
from webapp.persistence import cv_library as rows
from webapp.persistence import dbapi
from webapp.persistence.application_documents import create_document_version, new_document_version_id
from webapp.persistence.workspaces import ensure_profile_workspace
from webapp.services.document_blob_store import DocumentBlobStore

__all__ = [
    "DOCX_MEDIA_TYPE", "DocumentRejected", "LibraryError", "MAX_UPLOAD_BYTES", "PDF_MEDIA_TYPE", "add_version",
    "archive_item", "create_item", "get_item", "get_version", "latest_visible_version", "list_items",
    "list_versions", "read_version", "sniff_media_type", "unarchive_item", "usage_count", "validate_upload",
]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ORIGINS = ("USER_UPLOAD", "AI_GENERATED", "AI_TAILORED", "IMPORTED_LEGACY")
REJECTION_MESSAGES = {
    "EMPTY": "The file is empty.",
    "TOO_LARGE": "The file is larger than 10 MB.",
    "UNSUPPORTED_TYPE": "Upload a Word document (.docx) or a PDF.",
    "TYPE_MISMATCH": "The file's contents don't match its name (for example a PDF named .docx).",
    "INVALID_DOCX": "This Word document can't be read. Save it again as .docx and retry.",
    "INVALID_PDF": "This PDF can't be read. Export it again and retry.",
    "ENCRYPTED_PDF": "This PDF is password-protected. Remove the password and retry.",
}


class DocumentRejected(Exception):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(detail or REJECTION_MESSAGES[code])
        self.code = code
        self.message = REJECTION_MESSAGES[code]


class LibraryError(Exception):
    """Not found in this account, or not allowed in the item's state."""


def sniff_media_type(content: bytes) -> str | None:
    if content.startswith(b"%PDF-"):
        return PDF_MEDIA_TYPE
    if content.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if "[Content_Types].xml" in archive.namelist() and \
                        b"wordprocessingml" in archive.read("[Content_Types].xml"):
                    return DOCX_MEDIA_TYPE
        except (zipfile.BadZipFile, KeyError, OSError, ValueError):
            return None
    return None


def validate_upload(content: bytes, filename: str, media_type_hint: str | None = None) -> str:
    """The media type of an acceptable upload, or DocumentRejected. The
    browser's hint is never trusted; the bytes decide."""
    if not content:
        raise DocumentRejected("EMPTY")
    if len(content) > MAX_UPLOAD_BYTES:
        raise DocumentRejected("TOO_LARGE")
    extension = PurePath(filename or "").suffix.casefold()
    if extension not in MEDIA_TYPE_EXTENSIONS.values():
        raise DocumentRejected("UNSUPPORTED_TYPE")
    media_type = sniff_media_type(content)
    if media_type is None:
        raise DocumentRejected("UNSUPPORTED_TYPE")
    if MEDIA_TYPE_EXTENSIONS[media_type] != extension:
        raise DocumentRejected("TYPE_MISMATCH")
    if media_type == DOCX_MEDIA_TYPE:
        from product.docx_package import validate_docx_package
        try:
            validate_docx_package(content, original_filename=filename)
        except Exception as exc:  # noqa: BLE001 - every package failure is the same refusal
            raise DocumentRejected("INVALID_DOCX", str(exc)) from None
    else:
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise DocumentRejected("ENCRYPTED_PDF")
            if len(reader.pages) < 1:
                raise DocumentRejected("INVALID_PDF")
        except DocumentRejected:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DocumentRejected("INVALID_PDF", str(exc)) from None
    try:
        validate_display_filename(filename, media_type)
    except ValueError:
        raise DocumentRejected("UNSUPPORTED_TYPE") from None
    return media_type


# ---- items ------------------------------------------------------------------------------

def _title(value: str) -> str:
    title = " ".join((value or "").split())
    if not title or len(title) > 120:
        raise LibraryError("A CV needs a name of 1 to 120 characters.")
    return title


def create_item(conn: dbapi.Connection, scope: Any, *, title: str, description: str = "", now: datetime) -> dict:
    """No commit. The route checks the library.cv_items gauge first."""
    return rows.insert_item(conn, account_id=scope.account_id, title=_title(title),
                            description=" ".join((description or "").split())[:500], now=now)


def get_item(conn: dbapi.Connection, *, account_id: str, item_id: str) -> dict[str, Any] | None:
    return rows.get_item(conn, account_id=account_id, item_id=item_id)


def _owned(conn: dbapi.Connection, scope: Any, item_id: str) -> dict[str, Any]:
    item = rows.get_item(conn, account_id=scope.account_id, item_id=item_id)
    if item is None:
        raise LibraryError("CV not found")
    return item


def list_items(conn: dbapi.Connection, *, account_id: str) -> list[dict[str, Any]]:
    items = rows.list_items(conn, account_id=account_id)
    for item in items:
        latest = rows.latest_visible_version(conn, account_id=account_id, item_id=item["id"])
        item["latest_version_no"] = latest["version_no"] if latest else None
        item["latest_version_id"] = latest["id"] if latest else None
        item["latest_media_type"] = latest["media_type"] if latest else None
    return items


def archive_item(conn: dbapi.Connection, scope: Any, *, item_id: str, now: datetime) -> None:
    _owned(conn, scope, item_id)
    rows.set_status(conn, account_id=scope.account_id, item_id=item_id, status="ARCHIVED", now=now)


def unarchive_item(conn: dbapi.Connection, scope: Any, *, item_id: str, now: datetime) -> None:
    _owned(conn, scope, item_id)
    rows.set_status(conn, account_id=scope.account_id, item_id=item_id, status="ACTIVE", now=now)


# ---- versions ---------------------------------------------------------------------------

def add_version(conn: dbapi.Connection, scope: Any, *, item_id: str, content: bytes, filename: str,
                media_type_hint: str | None, documents_root: Path | Any, note: str = "", origin: str = "USER_UPLOAD",
                parent_version_id: str | None = None, template_id: str | None = None, library_visible: bool = True,
                generation_artifact_id: str | None = None, now: datetime) -> dict[str, Any]:
    """Validate → publish the blob → document version → library version (no
    commit). The route checks the storage.bytes gauge first."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown CV version origin {origin}")
    item = _owned(conn, scope, item_id)
    if item["status"] != "ACTIVE":
        raise LibraryError("This CV is archived. Restore it to add a version.")
    if parent_version_id is not None and rows.get_version(conn, account_id=scope.account_id,
                                                          version_id=parent_version_id) is None:
        raise LibraryError("parent version not found")
    media_type = validate_upload(content, filename, media_type_hint)
    blob = DocumentBlobStore(documents_root).publish(content, account_id=scope.account_id, media_type=media_type)
    profile = ensure_profile_workspace(conn, account_id=scope.account_id, commit=False)
    document = create_document_version(conn, {
        "id": new_document_version_id(), "account_id": scope.account_id, "source_workspace_id": profile["id"],
        "document_kind": "cv", "origin": "ai_generated" if generation_artifact_id else "user_uploaded",
        "original_filename": filename, "media_type": media_type, "byte_length": blob["byte_length"],
        "sha256": blob["sha256"], "storage_key": blob["storage_key"],
        "source_generation_artifact_id": generation_artifact_id, "created_at": now.isoformat(),
    }, commit=False)
    return rows.insert_version(conn, account_id=scope.account_id, item_id=item_id, document_version_id=document["id"],
                               origin=origin, parent_version_id=parent_version_id, template_id=template_id,
                               library_visible=library_visible, note=" ".join((note or "").split())[:500],
                               created_by=getattr(scope, "user_id", None) or scope.account_id, now=now)


def get_version(conn: dbapi.Connection, *, account_id: str, version_id: str) -> dict[str, Any] | None:
    return rows.get_version(conn, account_id=account_id, version_id=version_id)


def list_versions(conn: dbapi.Connection, *, account_id: str, item_id: str,
                  include_hidden: bool = False) -> list[dict[str, Any]]:
    return rows.list_versions(conn, account_id=account_id, item_id=item_id, include_hidden=include_hidden)


def latest_visible_version(conn: dbapi.Connection, *, account_id: str, item_id: str) -> dict[str, Any] | None:
    return rows.latest_visible_version(conn, account_id=account_id, item_id=item_id)


def usage_count(conn: dbapi.Connection, *, account_id: str, version_id: str) -> int:
    """How many approvals, fill runs and submission results use this version's exact bytes."""
    version = rows.get_version(conn, account_id=account_id, version_id=version_id)
    return 0 if version is None else rows.reference_count(conn, version["document_version_id"])


def read_version(conn: dbapi.Connection, *, account_id: str, version_id: str,
                 documents_root: Path | Any) -> tuple[dict[str, Any], bytes]:
    version = rows.get_version(conn, account_id=account_id, version_id=version_id)
    if version is None:
        raise LibraryError("CV version not found")
    return version, DocumentBlobStore(documents_root).read(version)
