from __future__ import annotations

import pytest

from nanodelta.security import hash_password, source_hash, token_hash, verify_password


def test_password_hash_is_salted_scrypt_and_verifies() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first.startswith("scrypt$")
    assert first != second
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("wrong password", first)


def test_tokens_and_sources_are_stored_as_one_way_hashes() -> None:
    assert token_hash("secret-token") != "secret-token"
    assert source_hash("203.0.113.7") != "203.0.113.7"
    assert len(token_hash("secret-token")) == 64


def test_password_policy_rejects_short_passwords() -> None:
    with pytest.raises(ValueError, match="12 characters"):
        hash_password("short")
