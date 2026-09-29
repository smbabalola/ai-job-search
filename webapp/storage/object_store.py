"""Object storage port (Bundle 7 spec H6, §10.4).

Keys are relative, slash-separated and validated; bytes are write-once
(no clobber). Document keys are tenant-prefixed, so identical content is never
deduplicated across accounts.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Protocol

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MEDIA_TYPE = "application/pdf"
MEDIA_TYPES = {DOCX_MEDIA_TYPE: "docx", PDF_MEDIA_TYPE: "pdf"}
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ObjectStoreError(RuntimeError):
    """A key was unsafe, missing, or would overwrite different bytes."""


def validate_key(key: str) -> str:
    segments = key.split("/") if isinstance(key, str) else []
    if not key or not segments or any(not _SEGMENT.fullmatch(s) or s in (".", "..") for s in segments):
        raise ObjectStoreError("unsafe object key")
    return key


def validate_prefix(prefix: str) -> str:
    if not prefix.endswith("/"):
        raise ObjectStoreError("an object prefix must end with '/'")
    validate_key(prefix[:-1])
    return prefix


def tenant_document_key(account_id: str, sha256: str, media_type: str) -> str:
    if not _ACCOUNT.fullmatch(account_id or ""):
        raise ValueError("account id must be a safe opaque identifier")
    if not _SHA256.fullmatch(sha256 or ""):
        raise ValueError("invalid document digest")
    extension = MEDIA_TYPES.get(media_type)
    if extension is None:
        raise ValueError(f"unsupported document media type {media_type!r}")
    return f"accounts/{account_id}/documents/sha256/{sha256[:2]}/{sha256}.{extension}"


class ObjectStore(Protocol):
    def put(self, key: str, content: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete_prefix(self, prefix: str) -> int: ...


class LocalFsObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        validate_key(key)
        root = os.path.abspath(str(self.root))
        path = os.path.abspath(os.path.join(root, *key.split("/")))
        if os.path.normcase(os.path.commonpath((root, path))) != os.path.normcase(root):
            raise ObjectStoreError("object path escaped storage root")
        return Path(path)

    def put(self, key: str, content: bytes) -> None:
        target = self._path(key)
        if target.exists():
            if target.read_bytes() != content:
                raise ObjectStoreError("object already exists with different content")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix=".put-", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != content:
                    raise ObjectStoreError("object already exists with different content") from None
            except OSError as exc:
                raise ObjectStoreError("object could not be written") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as exc:
            raise ObjectStoreError("object is unavailable") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete_prefix(self, prefix: str) -> int:
        validate_prefix(prefix)
        base = self._path(prefix[:-1])
        if not base.is_dir():
            return 0
        count = 0
        for path in sorted(base.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
                count += 1
            else:
                path.rmdir()
        base.rmdir()
        return count


class S3CompatibleObjectStore:
    """Any S3-compatible service (AWS, R2, MinIO, ...). ``prefix`` namespaces
    this deployment inside the bucket."""

    def __init__(self, *, bucket: str, endpoint_url: str | None, region: str | None, prefix: str = "",
                 client: Any = None) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3

            client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
        self.client = client

    def _full(self, key: str) -> str:
        validate_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, content: bytes) -> None:
        if self.exists(key):
            if self.get(key) != content:
                raise ObjectStoreError("object already exists with different content")
            return
        self.client.put_object(Bucket=self.bucket, Key=self._full(key), Body=content)

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._full(key))["Body"].read()
        except Exception as exc:
            raise ObjectStoreError("object is unavailable") from exc

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._full(key))
            return True
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise ObjectStoreError("object store is unavailable") from exc

    def delete_prefix(self, prefix: str) -> int:
        validate_prefix(prefix)
        full = self._full(prefix[:-1]) + "/"
        count = 0
        while True:
            listing = self.client.list_objects_v2(Bucket=self.bucket, Prefix=full)
            keys = [item["Key"] for item in listing.get("Contents", [])]
            if not keys:
                return count
            for start in range(0, len(keys), 1000):
                batch = keys[start:start + 1000]
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in batch]})
                count += len(batch)
            if not listing.get("IsTruncated"):
                return count


def object_store_from_settings(settings: Any, *, s3_client: Any = None) -> ObjectStore:
    config = settings.object_store or {"kind": "local"}
    if config.get("kind") == "s3":
        return S3CompatibleObjectStore(bucket=config["bucket"], endpoint_url=config.get("endpoint_url"),
                                       region=config.get("region"), prefix=config.get("prefix", ""),
                                       client=s3_client)
    root = config.get("root")
    return LocalFsObjectStore(Path(root) if root else Path(settings.documents_root) / "document_blobs")
