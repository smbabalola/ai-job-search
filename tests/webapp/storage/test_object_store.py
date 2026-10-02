"""Bundle 7 Task 4: object store port and tenant-prefixed document blobs (spec H6, §10.4)."""
from __future__ import annotations

import hashlib

import pytest

from webapp.services.document_blob_store import DocumentBlobError, DocumentBlobStore
from webapp.storage.object_store import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    LocalFsObjectStore,
    ObjectStoreError,
    S3CompatibleObjectStore,
    object_store_from_settings,
    tenant_document_key,
)


class FakeS3Client:
    """In-memory stand-in for the boto3 S3 client calls the adapter uses."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket, Key, Body, **_):
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey({}, "GetObject")
        body = self.objects[(Bucket, Key)]
        return {"Body": type("B", (), {"read": lambda _self: body})()}

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, *, Bucket, Prefix, **_):
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def delete_objects(self, *, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop((Bucket, item["Key"]), None)
        return {}

    class exceptions:
        class NoSuchKey(Exception):
            pass


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalFsObjectStore(tmp_path / "objects")
    return S3CompatibleObjectStore(bucket="b", endpoint_url=None, region="eu-west-2", prefix="prod",
                                   client=FakeS3Client())


def test_put_get_exists_and_no_clobber(store):
    store.put("accounts/a/x.bin", b"one")
    assert store.exists("accounts/a/x.bin") and store.get("accounts/a/x.bin") == b"one"
    store.put("accounts/a/x.bin", b"one")  # identical content is fine
    with pytest.raises(ObjectStoreError):
        store.put("accounts/a/x.bin", b"two")
    assert not store.exists("accounts/a/missing")
    with pytest.raises(ObjectStoreError):
        store.get("accounts/a/missing")


@pytest.mark.parametrize("key", ["../x", "accounts/../../x", "/abs/x", "accounts/a/..", ""])
def test_unsafe_keys_are_refused(store, key):
    with pytest.raises(ObjectStoreError):
        store.put(key, b"x")


def test_delete_prefix_removes_only_that_prefix(store):
    for key in ("accounts/a/1", "accounts/a/2", "accounts/ab/3"):
        store.put(key, b"x")
    assert store.delete_prefix("accounts/a/") == 2
    assert store.exists("accounts/ab/3") and not store.exists("accounts/a/1")


def test_tenant_document_key_layout():
    digest = "ab" + "0" * 62
    assert tenant_document_key("acct_1", digest, DOCX_MEDIA_TYPE) == f"accounts/acct_1/documents/sha256/ab/{digest}.docx"
    assert tenant_document_key("acct_1", digest, PDF_MEDIA_TYPE).endswith(".pdf")
    with pytest.raises(ValueError):
        tenant_document_key("acct_1", digest, "text/plain")
    with pytest.raises(ValueError):
        tenant_document_key("../x", digest, DOCX_MEDIA_TYPE)


def test_object_store_from_settings(tmp_path):
    from webapp.config import Settings

    local = object_store_from_settings(Settings(documents_root=tmp_path))
    assert isinstance(local, LocalFsObjectStore)
    s3 = object_store_from_settings(Settings(object_store={"kind": "s3", "bucket": "b", "region": "r",
                                                             "endpoint_url": None, "prefix": "p"}),
                                    s3_client=FakeS3Client())
    assert isinstance(s3, S3CompatibleObjectStore)


# ---- DocumentBlobStore ----------------------------------------------------

def test_blobs_are_tenant_prefixed_and_never_shared_across_accounts(tmp_path):
    blobs = DocumentBlobStore(tmp_path)
    a = blobs.publish(b"same bytes", account_id="acct_a", media_type=DOCX_MEDIA_TYPE)
    b = blobs.publish(b"same bytes", account_id="acct_b", media_type=DOCX_MEDIA_TYPE)
    digest = hashlib.sha256(b"same bytes").hexdigest()
    assert a["storage_key"] == f"accounts/acct_a/documents/sha256/{digest[:2]}/{digest}.docx"
    assert b["storage_key"].startswith("accounts/acct_b/")
    assert blobs.read(a) == b"same bytes" and blobs.read(b) == b"same bytes"


def test_legacy_blob_keys_still_read(tmp_path):
    content = b"legacy docx"
    digest = hashlib.sha256(content).hexdigest()
    legacy = tmp_path / "document_blobs" / "sha256" / digest[:2] / f"{digest}.docx"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(content)
    record = {"storage_key": f"sha256/{digest[:2]}/{digest}.docx", "byte_length": len(content), "sha256": digest}
    assert DocumentBlobStore(tmp_path).read(record) == content


def test_integrity_mismatch_is_refused(tmp_path):
    blobs = DocumentBlobStore(tmp_path)
    record = blobs.publish(b"real", account_id="acct_a", media_type=PDF_MEDIA_TYPE)
    with pytest.raises(DocumentBlobError):
        blobs.read({**record, "byte_length": 999})
    with pytest.raises(DocumentBlobError):
        blobs.read({**record, "storage_key": "accounts/acct_a/../../etc/passwd"})


def test_blob_store_over_an_object_store(tmp_path):
    object_store = S3CompatibleObjectStore(bucket="b", endpoint_url=None, region="r", prefix="p",
                                           client=FakeS3Client())
    blobs = DocumentBlobStore(object_store)
    record = blobs.publish(b"cv", account_id="acct_a", media_type=DOCX_MEDIA_TYPE)
    assert blobs.read(record) == b"cv"
