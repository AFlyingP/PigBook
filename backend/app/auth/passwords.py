import email_validator
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

# Spec 8.1: Argon2id via argon2-cffi with memory_cost 65536 KiB, time_cost 3, parallelism 2
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # KiB
ARGON2_PARALLELISM = 2

password_hasher = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST,
    parallelism=ARGON2_PARALLELISM,
    type=Type.ID,
)

# Dummy hash used for constant-time dummy verification when account does not exist
_DUMMY_HASH: str = password_hasher.hash("commonsbook-dummy-timing-password-not-real")


def validate_password_length(password: str) -> str:
    """Enforce password length 12..128 Unicode codepoints (no silent truncation)."""
    codepoints = len(password)
    if codepoints < 12 or codepoints > 128:
        raise ValueError(
            f"Password length must be between 12 and 128 Unicode codepoints (got {codepoints})"
        )
    return password


def hash_password(password: str) -> str:
    """Validate password length and return Argon2id hash."""
    validate_password_length(password)
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against Argon2id hash. Returns True on match, False otherwise."""
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def verify_dummy_password(password: str) -> bool:
    """Dummy-hash verification path so that login timing does not reveal account existence."""
    try:
        password_hasher.verify(_DUMMY_HASH, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def normalize_email(email: str) -> str:
    """Normalize and validate email: strip, lowercase, max 254 codepoints."""
    cleaned = email.strip().lower()
    if not cleaned or len(cleaned) > 254:
        raise ValueError("Email length must be between 1 and 254 characters")
    try:
        info = email_validator.validate_email(cleaned, check_deliverability=False)
        normalized = info.normalized.lower()
        if len(normalized) > 254:
            raise ValueError("Email length must not exceed 254 characters")
        return normalized
    except email_validator.EmailNotValidError as exc:
        raise ValueError(f"Invalid email address: {exc}") from exc
