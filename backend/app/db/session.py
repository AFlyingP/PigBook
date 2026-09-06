import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

__all__ = ["get_session", "transaction_dependency"]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_engine_url: str | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker, _engine_url
    url = get_settings().DATABASE_URL or os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    if _engine is None or _engine_url != url:
        _engine = create_async_engine(
            url,
            connect_args={
                "server_settings": {
                    "timezone": "UTC",
                    "lock_timeout": "15s",
                    "statement_timeout": "30s",
                    "idle_in_transaction_session_timeout": "30s",
                }
            },
        )
        _engine_url = url
        _sessionmaker = async_sessionmaker(
            bind=_engine,
            expire_on_commit=False,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session


async def transaction_dependency() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            yield session
