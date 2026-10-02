"""No-clobber, integrity-verified storage for authoritative document bytes
(DOCX and PDF), over the ObjectStore port (Bundle 7 spec H6, §10.4).

New keys are tenant-prefixed (``accounts/{id}/documents/sha256/..``), so the
same bytes uploaded by two accounts are stored twice. Pre-Bundle-7 keys
(``sha256/{aa}/{digest}.docx``) remain readable."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from webapp.storage.object_store import (
    DOCX_MEDIA_TYPE,
    LocalFsObjectStore,
    ObjectStore,
    ObjectStoreError,
    tenant_document_key,
)

_LEGACY_KEY = re.compile(r"sha256/([0-9a-f]{2})/([0-9a-f]{64})\.docx\Z")
_TENANT_KEY = re.compile(r"accounts/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/documents/sha256/([0-9a-f]{2})/([0-9a-f]{64})\.(docx|pdf)\Z")


class DocumentBlobError(RuntimeError):
    """Raised when authoritative document bytes cannot be safely stored/read."""


def _checked_key(storage_key: str) -> str:
    match = _LEGACY_KEY.fullmatch(storage_key or "") or _TENANT_KEY.fullmatch(storage_key or "")
    if match is None or match.group(1) != match.group(2)[:2]:
        raise DocumentBlobError("invalid document storage key")
    return storage_key


class DocumentBlobStore:
    def __init__(self, target: "Path | str | ObjectStore"):
        """target: a documents root directory (local mode) or an ObjectStore."""
        if isinstance(target, (str, Path)):
            target = LocalFsObjectStore(Path(target) / "document_blobs")
        self.objects: ObjectStore = target

    def publish(self, content: bytes, *, account_id: str, media_type: str = DOCX_MEDIA_TYPE) -> dict[str, Any]:
        if not isinstance(content, bytes) or not content:
            raise DocumentBlobError("document content must be non-empty bytes")
        digest = hashlib.sha256(content).hexdigest()
        try:
            key = tenant_document_key(account_id, digest, media_type)
            self.objects.put(key, content)
        except (ValueError, ObjectStoreError) as exc:
            raise DocumentBlobError("document bytes could not be published") from exc
        return {"storage_key": key, "byte_length": len(content), "sha256": digest}

    def read(self, document: dict[str, Any]) -> bytes:
        key = _checked_key(document["storage_key"])
        try:
            content = self.objects.get(key)
        except ObjectStoreError as exc:
            raise DocumentBlobError("document bytes are unavailable") from exc
        if len(content) != document["byte_length"] or hashlib.sha256(content).hexdigest() != document["sha256"]:
            raise DocumentBlobError("stored document failed integrity verification")
        return content
