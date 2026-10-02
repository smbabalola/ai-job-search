"""Encryption of small secrets at rest (Bundle 7 spec §6.1 staff_totp, T16).

AES-256-GCM from the ``cryptography`` package (vetted authenticated
encryption) under a key ring from hosted secret configuration:
``JOBSEARCH_TOTP_ENCRYPTION_KEYS`` = ``kid:base64url-32-byte-key[,kid:key...]``.
The first key encrypts; every listed key decrypts, so keys rotate by putting
a new key first and re-encrypting (``needs_rotation`` / ``reencrypt``).

A record is ``v2.<kid>.<base64url(nonce ‖ ciphertext ‖ tag)>``; the caller's
``purpose`` (e.g. the user id) is bound as associated data, so a record moved
to another row does not decrypt. A missing ring in hosted mode, an unknown
kid, a wrong key or any tampering raise ``SecretBoxError`` — never a partial
or default value. Local development without a configured ring uses a fixed,
clearly named development key that hosted mode refuses."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["KeyRing", "SecretBoxError", "decrypt", "encrypt", "key_ring", "needs_rotation", "parse_key_ring",
           "reencrypt"]

VERSION = "v2"
DEV_KID = "dev-local"
_DEV_KEY = b"jobsearch-local-dev-totp-key-32b"  # 32 bytes; local mode only, refused in hosted


class SecretBoxError(Exception):
    """The record cannot be produced or opened (no key, unknown key id, wrong key, tampered)."""


@dataclass(frozen=True)
class KeyRing:
    active_kid: str
    keys: dict[str, bytes]


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_key_ring(spec: str) -> KeyRing:
    keys: dict[str, bytes] = {}
    order: list[str] = []
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        kid, sep, encoded = part.partition(":")
        if not sep or not kid or "." in kid:
            raise SecretBoxError("each key must be kid:base64url-key with a kid that has no '.'")
        try:
            key = _b64decode(encoded)
        except ValueError as exc:
            raise SecretBoxError(f"key {kid} is not base64url") from exc
        if len(key) != 32:
            raise SecretBoxError(f"key {kid} must be 32 bytes (AES-256)")
        if kid in keys:
            raise SecretBoxError(f"duplicate key id {kid}")
        keys[kid] = key
        order.append(kid)
    if not order:
        raise SecretBoxError("the key ring is empty")
    return KeyRing(active_kid=order[0], keys=keys)


def key_ring(settings: Any) -> KeyRing:
    spec = getattr(settings, "totp_encryption_keys", None)
    if spec:
        return parse_key_ring(spec)
    if settings.is_hosted:
        raise SecretBoxError("JOBSEARCH_TOTP_ENCRYPTION_KEYS is required in hosted mode")
    return KeyRing(active_kid=DEV_KID, keys={DEV_KID: _DEV_KEY})


def encrypt(settings: Any, plaintext: str, *, purpose: str) -> str:
    ring = key_ring(settings)
    nonce = os.urandom(12)
    sealed = AESGCM(ring.keys[ring.active_kid]).encrypt(nonce, plaintext.encode("utf-8"),
                                                        f"{purpose}|{ring.active_kid}".encode())
    return f"{VERSION}.{ring.active_kid}.{base64.urlsafe_b64encode(nonce + sealed).decode().rstrip('=')}"


def _split(record: str) -> tuple[str, bytes]:
    version, _, rest = record.partition(".")
    kid, _, body = rest.partition(".")
    if version != VERSION or not kid or not body:
        raise SecretBoxError("unknown secret record format")
    try:
        return kid, _b64decode(body)
    except ValueError as exc:
        raise SecretBoxError("the secret record is corrupt") from exc


def decrypt(settings: Any, record: str, *, purpose: str) -> str:
    kid, raw = _split(record)
    key = key_ring(settings).keys.get(kid)
    if key is None:
        raise SecretBoxError(f"no key {kid} in the key ring")
    if len(raw) < 12 + 16:
        raise SecretBoxError("the secret record is corrupt")
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], f"{purpose}|{kid}".encode()).decode("utf-8")
    except InvalidTag as exc:
        raise SecretBoxError("the secret record failed authentication") from exc


def needs_rotation(settings: Any, record: str) -> bool:
    return _split(record)[0] != key_ring(settings).active_kid


def reencrypt(settings: Any, record: str, *, purpose: str) -> str:
    return encrypt(settings, decrypt(settings, record, purpose=purpose), purpose=purpose)
