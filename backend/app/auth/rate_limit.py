import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import text

from app.auth.passwords import normalize_email
from app.config import get_settings
from app.db.session import get_sessionmaker


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded. Retry after {retry_after} seconds.")


@dataclass(frozen=True)
class RateLimitBucket:
    scope: str
    identity_hash: str
    window_start: datetime
    limit: int
    window_seconds: int


def hash_identity(raw_identity: str) -> str:
    """Hash an identity string with RATE_LIMIT_HMAC_SECRET using HMAC-SHA256 (64 hex chars)."""
    settings = get_settings()
    secret = settings.RATE_LIMIT_HMAC_SECRET
    if not secret or len(secret.encode("utf-8")) < 32:
        raise RuntimeError("RATE_LIMIT_HMAC_SECRET must be configured and at least 32 bytes")
    return hmac.new(
        secret.encode("utf-8"),
        raw_identity.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def get_client_ip(request: Request) -> str:
    """Extract client IP: trusts proxy header with hop count 1 in prod;
    socket peer in local/test.
    """
    settings = get_settings()
    if settings.APP_ENV in ("local", "test"):
        if request.client and request.client.host:
            return request.client.host
        return "127.0.0.1"

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


async def consume_rate_limits(buckets: list[RateLimitBucket]) -> None:
    """Consume all applicable buckets in ONE SHORT INDEPENDENT TRANSACTION,

    committed BEFORE domain transaction runs. Buckets are locked in sorted
    (scope, identity_hash, window_start) order to prevent deadlocks.
    """
    if not buckets:
        return

    # Acquire buckets in sorted (scope, identity_hash, window_start) order
    sorted_buckets = sorted(
        buckets,
        key=lambda b: (b.scope, b.identity_hash, b.window_start),
    )

    max_retry_after: int | None = None
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as rate_session:
        async with rate_session.begin():
            for bucket in sorted_buckets:
                stmt = text(
                    """
                    INSERT INTO rate_limits (scope, identity_hash, window_start, count)
                    VALUES (:scope, :identity_hash, :window_start, 1)
                    ON CONFLICT (scope, identity_hash, window_start)
                    DO UPDATE SET count = rate_limits.count + 1
                    RETURNING count;
                    """
                )
                res = await rate_session.execute(
                    stmt,
                    {
                        "scope": bucket.scope,
                        "identity_hash": bucket.identity_hash,
                        "window_start": bucket.window_start,
                    },
                )
                current_count = res.scalar_one()
                if current_count > bucket.limit:
                    now_epoch = int(datetime.now(timezone.utc).timestamp())
                    seconds_left = bucket.window_seconds - (now_epoch % bucket.window_seconds)
                    retry_after = max(1, seconds_left)
                    if max_retry_after is None or retry_after > max_retry_after:
                        max_retry_after = retry_after

    if max_retry_after is not None:
        raise RateLimitExceeded(retry_after=max_retry_after)


async def check_login_rate_limit(
    request: Request,
    email: str,
    now: datetime | None = None,
) -> None:
    """Consume rate limits for login: 10/IP/min and 5/normalized-email/min."""
    if now is None:
        now = datetime.now(timezone.utc)

    client_ip = get_client_ip(request)
    ip_hash = hash_identity(client_ip)

    try:
        norm_email = normalize_email(email)
    except ValueError:
        norm_email = email.strip().lower()

    email_hash = hash_identity(norm_email)

    timestamp = int(now.timestamp())
    window_seconds = 60
    start_epoch = timestamp - (timestamp % window_seconds)
    window_start = datetime.fromtimestamp(start_epoch, tz=timezone.utc)

    buckets = [
        RateLimitBucket(
            scope="login:ip",
            identity_hash=ip_hash,
            window_start=window_start,
            limit=10,
            window_seconds=60,
        ),
        RateLimitBucket(
            scope="login:email",
            identity_hash=email_hash,
            window_start=window_start,
            limit=5,
            window_seconds=60,
        ),
    ]
    await consume_rate_limits(buckets)
