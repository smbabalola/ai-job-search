"""Password policy and hashing (Bundle 7 spec A2): argon2id, 12-128
characters, not on the bundled top-10k common-password list."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 128
_COMMON_PASSWORDS = Path(__file__).resolve().parents[2] / "product" / "common_passwords.txt"
_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters


@lru_cache(maxsize=1)
def _common() -> frozenset[str]:
    return frozenset(
        line.strip() for line in _COMMON_PASSWORDS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


def password_problems(password: str) -> list[str]:
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        return ["too_short"]
    if len(password) > MAX_LENGTH:
        return ["too_long"]
    if password.lower() in _common():
        return ["too_common"]
    return []


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# Verified against for unknown emails so a miss costs the same as a hit (spec §7).
DUMMY_PASSWORD_HASH = _hasher.hash("jobsearch-dummy-password-never-matches")
