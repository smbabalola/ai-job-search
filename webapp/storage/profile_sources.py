"""Candidate profile source storage (Bundle 7 spec H7, §10.4).

The Markdown/LaTeX evidence sources (``product.profile_snapshot.SOURCE_PATHS``)
live on disk in local mode and in ``profile_source_revisions`` in hosted mode.
Callers get a handle bound to one account (and, for the database backend, to
the caller's connection, so a source write commits or rolls back with the
caller's transaction).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from product.profile_snapshot import SOURCE_PATHS


class ProfileSourceConflict(RuntimeError):
    """The source changed since the revision the caller read."""


class ProfileSources(Protocol):
    def read(self, source_path: str) -> str | None: ...
    def digest(self, source_path: str) -> str | None: ...
    def write(self, source_path: str, text: str, *, expected_revision: int | None = None) -> int: ...
    def delete(self, source_path: str) -> None: ...
    def revision(self, source_path: str) -> int: ...
    def reader(self) -> "ProfileSources": ...


def _check_path(source_path: str) -> str:
    if source_path not in SOURCE_PATHS:
        raise ValueError(f"unsupported profile source {source_path!r}")
    return source_path


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FilesystemProfileSources:
    """Sources under one account's profile root (the pre-Bundle-7 layout).
    Store-level revisions are not tracked on disk: ``revision`` is 1 when the
    file exists, and optimistic concurrency uses content digests instead."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, source_path: str) -> Path:
        return self.root / _check_path(source_path)

    def read(self, source_path: str) -> str | None:
        """Exact file content (no newline translation), so a restore is byte-faithful."""
        path = self._path(source_path)
        return path.read_bytes().decode("utf-8") if path.is_file() else None

    def digest(self, source_path: str) -> str | None:
        path = self._path(source_path)
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    def write(self, source_path: str, text: str, *, expected_revision: int | None = None) -> int:
        if expected_revision is not None and expected_revision != self.revision(source_path):
            raise ProfileSourceConflict(f"{source_path} changed")
        target = self._path(source_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".source-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return 1

    def delete(self, source_path: str) -> None:
        self._path(source_path).unlink(missing_ok=True)

    def revision(self, source_path: str) -> int:
        return 1 if self._path(source_path).is_file() else 0

    def reader(self):
        """The snapshot reader: universal-newline text, exactly as build_snapshot
        has always read files from a profile root."""
        from product.profile_snapshot import FilesystemSourceReader

        return FilesystemSourceReader(self.root)


class DatabaseProfileSources:
    """Append-only revisions in ``profile_source_revisions``; a deletion is a
    revision whose content is NULL. Never commits: the caller owns the
    transaction."""

    def __init__(self, conn: Any, account_id: str) -> None:
        self.conn = conn
        self.account_id = account_id

    def _latest(self, source_path: str):
        return self.conn.execute(
            "SELECT revision, content, sha256 FROM profile_source_revisions "
            "WHERE account_id = ? AND source_path = ? ORDER BY revision DESC LIMIT 1",
            (self.account_id, _check_path(source_path)),
        ).fetchone()

    def read(self, source_path: str) -> str | None:
        row = self._latest(source_path)
        return row["content"] if row else None

    def digest(self, source_path: str) -> str | None:
        row = self._latest(source_path)
        return row["sha256"] if row and row["content"] is not None else None

    def revision(self, source_path: str) -> int:
        row = self._latest(source_path)
        return row["revision"] if row else 0

    def _append(self, source_path: str, text: str | None, expected_revision: int | None) -> int:
        current = self.revision(source_path)
        if expected_revision is not None and expected_revision != current:
            raise ProfileSourceConflict(f"{source_path} changed")
        self.conn.execute(
            "INSERT INTO profile_source_revisions "
            "(id, account_id, source_path, revision, content, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"psrc_{uuid.uuid4().hex[:20]}", self.account_id, source_path, current + 1, text,
             None if text is None else _sha256(text), datetime.now(timezone.utc).isoformat()),
        )
        return current + 1

    def write(self, source_path: str, text: str, *, expected_revision: int | None = None) -> int:
        return self._append(_check_path(source_path), text, expected_revision)

    def delete(self, source_path: str) -> None:
        if self.read(source_path) is not None:
            self._append(_check_path(source_path), None, None)

    def reader(self) -> "DatabaseProfileSources":
        return self


class FilesystemProfileSourceStore:
    def __init__(self, base_root: str | Path) -> None:
        self.base_root = Path(base_root)

    def for_account(self, conn: Any, account_id: str) -> FilesystemProfileSources:
        from webapp.services.ownership import account_profile_root

        return FilesystemProfileSources(account_profile_root(self.base_root, account_id))


class DatabaseProfileSourceStore:
    def for_account(self, conn: Any, account_id: str) -> DatabaseProfileSources:
        return DatabaseProfileSources(conn, account_id)


def profile_source_store_from_settings(settings: Any):
    """Signed-up accounts keep their sources in the database; only the legacy
    local single-user mode reads and writes files under profile_root."""
    if settings.auth_enabled:
        return DatabaseProfileSourceStore()
    return FilesystemProfileSourceStore(settings.profile_root)


def as_profile_sources(root_or_sources: Any) -> ProfileSources:
    """Accept a profile root path (legacy callers) or an already-bound handle."""
    if isinstance(root_or_sources, (str, Path)):
        return FilesystemProfileSources(Path(root_or_sources))
    return root_or_sources
