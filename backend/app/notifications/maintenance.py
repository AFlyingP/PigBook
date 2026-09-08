"""Hourly background maintenance tasks.

Spec 6.2:
Runs hourly, in batches of 500, deleting only:
- expired idempotency rows (idempotency_keys.expires_at <= clock_timestamp())
- rate-limit buckets older than 24 hours (rate_limits.window_start <
  clock_timestamp() - interval '24 hours')

It must not delete bookings, users, feedback, audit rows, outbox rows or
refresh-token families. Loop in bounded batches until a batch affects fewer than
500 rows; do not hold one long transaction.
"""

from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import RateLimit
from app.bookings.models import IdempotencyKey

BATCH_SIZE: int = 500


async def purge_expired_idempotency_keys_batch(
    session: AsyncSession,
    *,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Delete a single bounded batch of expired idempotency keys."""
    subq = (
        select(IdempotencyKey.user_id, IdempotencyKey.key)
        .where(IdempotencyKey.expires_at <= func.clock_timestamp())
        .limit(batch_size)
    ).subquery()

    stmt = delete(IdempotencyKey).where(
        tuple_(IdempotencyKey.user_id, IdempotencyKey.key).in_(select(subq.c.user_id, subq.c.key))
    )
    result = await session.execute(stmt)
    return getattr(result, "rowcount", 0) or 0


async def purge_expired_rate_limits_batch(
    session: AsyncSession,
    *,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Delete a single bounded batch of rate limit buckets older than 24 hours."""
    cutoff = func.clock_timestamp() - text("interval '24 hours'")
    subq = (
        select(RateLimit.scope, RateLimit.identity_hash, RateLimit.window_start)
        .where(RateLimit.window_start < cutoff)
        .limit(batch_size)
    ).subquery()

    stmt = delete(RateLimit).where(
        tuple_(RateLimit.scope, RateLimit.identity_hash, RateLimit.window_start).in_(
            select(subq.c.scope, subq.c.identity_hash, subq.c.window_start)
        )
    )
    result = await session.execute(stmt)
    return getattr(result, "rowcount", 0) or 0


async def purge_expired_idempotency_keys(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Purge expired idempotency keys in bounded batches across short transactions."""
    total_deleted = 0
    while True:
        async with sessionmaker() as session:
            async with session.begin():
                deleted = await purge_expired_idempotency_keys_batch(session, batch_size=batch_size)
        total_deleted += deleted
        if deleted < batch_size:
            break
    return total_deleted


async def purge_expired_rate_limits(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Purge old rate limit buckets in bounded batches across short transactions."""
    total_deleted = 0
    while True:
        async with sessionmaker() as session:
            async with session.begin():
                deleted = await purge_expired_rate_limits_batch(session, batch_size=batch_size)
        total_deleted += deleted
        if deleted < batch_size:
            break
    return total_deleted


async def run_maintenance(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = BATCH_SIZE,
) -> dict[str, int]:
    """Execute all hourly maintenance operations."""
    idempotency_count = await purge_expired_idempotency_keys(sessionmaker, batch_size=batch_size)
    rate_limit_count = await purge_expired_rate_limits(sessionmaker, batch_size=batch_size)
    return {
        "idempotency_keys": idempotency_count,
        "rate_limits": rate_limit_count,
    }
