import pytest
from argon2 import Type

from app.auth.passwords import (
    ARGON2_MEMORY_COST,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
    hash_password,
    normalize_email,
    password_hasher,
    validate_password_length,
    verify_dummy_password,
    verify_password,
)


def test_argon2id_exact_parameters() -> None:
    """Argon2id parameters are exactly memory_cost=65536, time_cost=3, parallelism=2."""
    assert ARGON2_MEMORY_COST == 65536
    assert ARGON2_TIME_COST == 3
    assert ARGON2_PARALLELISM == 2
    assert password_hasher.memory_cost == 65536
    assert password_hasher.time_cost == 3
    assert password_hasher.parallelism == 2
    assert password_hasher.type == Type.ID

    h = hash_password("valid-password-123")
    # Verify hash header format: $argon2id$v=19$m=65536,t=3,p=2$
    assert h.startswith("$argon2id$")
    assert "m=65536,t=3,p=2" in h


def test_password_boundary_lengths() -> None:
    """Password boundary lengths: 11 rejected, 12 accepted, 128 accepted, 129 rejected,

    counted in Unicode codepoints (including multi-byte / astral characters).
    """
    # 11 characters - rejected
    with pytest.raises(ValueError, match="between 12 and 128"):
        validate_password_length("a" * 11)

    # 12 characters - accepted
    assert validate_password_length("a" * 12) == "a" * 12

    # 128 characters - accepted
    assert validate_password_length("a" * 128) == "a" * 128

    # 129 characters - rejected
    with pytest.raises(ValueError, match="between 12 and 128"):
        validate_password_length("a" * 129)

    # Astral / multi-byte characters: 11 ascii + 1 astral char (🚀) = 12 codepoints (accepted)
    astral_12 = "a" * 11 + "🚀"
    assert len(astral_12) == 12  # codepoints
    assert len(astral_12.encode("utf-8")) == 15  # bytes > 12
    assert validate_password_length(astral_12) == astral_12

    # Astral / multi-byte characters: 10 ascii + 1 astral char = 11 codepoints (rejected)
    astral_11 = "a" * 10 + "🚀"
    assert len(astral_11) == 11
    with pytest.raises(ValueError, match="between 12 and 128"):
        validate_password_length(astral_11)

    # Astral / multi-byte characters: 127 ascii + 1 astral char = 128 codepoints (accepted)
    astral_128 = "a" * 127 + "🚀"
    assert len(astral_128) == 128
    assert validate_password_length(astral_128) == astral_128

    # Astral / multi-byte characters: 128 ascii + 1 astral char = 129 codepoints (rejected)
    astral_129 = "a" * 128 + "🚀"
    assert len(astral_129) == 129
    with pytest.raises(ValueError, match="between 12 and 128"):
        validate_password_length(astral_129)


def test_email_normalization() -> None:
    """Email normalization strips, lowercases, and rejects over-254 characters."""
    assert normalize_email("  User@Example.COM  ") == "user@example.com"
    assert normalize_email("alice.smith+tag@sub.domain.org") == "alice.smith+tag@sub.domain.org"

    # Empty or whitespace only
    with pytest.raises(ValueError):
        normalize_email("   ")

    # Invalid syntax
    with pytest.raises(ValueError):
        normalize_email("not-an-email")

    # Over 254 characters
    long_local = "a" * 245
    long_email = f"{long_local}@example.com"  # 245 + 12 = 257 chars
    assert len(long_email) > 254
    with pytest.raises(ValueError):
        normalize_email(long_email)


def test_verify_password() -> None:
    """Verify rejects a wrong password and accepts the right one."""
    pwd = "correct-horse-battery-staple"
    h = hash_password(pwd)
    assert verify_password(pwd, h) is True
    assert verify_password("wrong-password-1234", h) is False
    assert verify_password("", h) is False


def test_dummy_hash_verification() -> None:
    """Dummy-hash verification path exists and consistently returns False."""
    assert verify_dummy_password("any-password-attempt") is False
    assert verify_dummy_password("") is False
