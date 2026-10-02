"""Bundle 7 spec A2: argon2id passwords, 12-128 characters, not a common password."""
from __future__ import annotations

from webapp.services.passwords import DUMMY_PASSWORD_HASH, hash_password, password_problems, verify_password


def test_password_problems():
    assert password_problems("short") == ["too_short"]
    assert password_problems("x" * 129) == ["too_long"]
    assert password_problems("x" * 12) == []
    assert password_problems("contortionist") == ["too_common"]
    assert password_problems("ContortionisT") == ["too_common"]  # case-insensitive list match
    assert password_problems("correct horse battery staple") == []


def test_hash_and_verify():
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("$argon2id$")
    assert verify_password(stored, "correct horse battery staple") is True
    assert verify_password(stored, "Correct horse battery staple") is False
    assert verify_password("not-a-hash", "anything") is False


def test_dummy_hash_verifies_nothing_but_costs_the_same():
    assert DUMMY_PASSWORD_HASH.startswith("$argon2id$")
    assert verify_password(DUMMY_PASSWORD_HASH, "") is False
