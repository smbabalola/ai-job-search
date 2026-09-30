"""Pure contracts for immutable application-document metadata."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


DOCUMENT_KINDS = frozenset({"cv", "cover_letter"})
DOCUMENT_ORIGINS = frozenset({"ai_generated", "user_uploaded"})
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
PDF_MEDIA_TYPE = "application/pdf"
# Bundle 7 §14.1: the two allowed document types and their filename extensions.
MEDIA_TYPE_EXTENSIONS = {DOCX_MEDIA_TYPE: ".docx", PDF_MEDIA_TYPE: ".pdf"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION_KEYS = {
    "id", "account_id", "source_workspace_id", "document_kind", "origin",
    "original_filename", "media_type", "byte_length", "sha256", "storage_key",
    "source_generation_artifact_id", "created_at",
}


class ApplicationDocumentContractError(ValueError):
    """Raised when immutable application-document metadata is invalid."""


def validate_display_filename(value: Any, media_type: str = DOCX_MEDIA_TYPE) -> str:
    if not isinstance(value, str):
        raise ApplicationDocumentContractError("original filename must be a string")
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > 255 or any(ord(ch) < 32 or ch in "\r\n" for ch in value):
        raise ApplicationDocumentContractError("original filename is invalid")
    extension = MEDIA_TYPE_EXTENSIONS.get(media_type)
    if extension is None:
        raise ApplicationDocumentContractError("document media type is invalid")
    if "/" in value or "\\" in value or not value.casefold().endswith(extension):
        label = "DOCX" if media_type == DOCX_MEDIA_TYPE else "PDF"
        raise ApplicationDocumentContractError(f"original filename must be a safe {label} name")
    return value

def validate_document_version(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _VERSION_KEYS:
        raise ApplicationDocumentContractError("document version has invalid keys")
    for field in ("id", "account_id", "source_workspace_id", "storage_key", "created_at"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ApplicationDocumentContractError(f"{field} must be a non-empty string")
    if value["document_kind"] not in DOCUMENT_KINDS:
        raise ApplicationDocumentContractError("document kind is invalid")
    if value["origin"] not in DOCUMENT_ORIGINS:
        raise ApplicationDocumentContractError("document origin is invalid")
    if value["media_type"] not in MEDIA_TYPE_EXTENSIONS:
        raise ApplicationDocumentContractError("document media type is invalid")
    validate_display_filename(value["original_filename"], value["media_type"])
    if not isinstance(value["byte_length"], int) or isinstance(value["byte_length"], bool) or value["byte_length"] <= 0:
        raise ApplicationDocumentContractError("byte length must be a positive integer")
    if not isinstance(value["sha256"], str) or _SHA256.fullmatch(value["sha256"]) is None:
        raise ApplicationDocumentContractError("sha256 must be lowercase hexadecimal")
    generation_id = value["source_generation_artifact_id"]
    if value["origin"] == "ai_generated":
        if not isinstance(generation_id, str) or not generation_id.strip():
            raise ApplicationDocumentContractError("AI-generated documents require generation provenance")
    elif generation_id is not None:
        raise ApplicationDocumentContractError("user uploads cannot claim generation provenance")
    return value
