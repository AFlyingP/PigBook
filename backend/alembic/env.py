import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# Import Base and models to populate target_metadata
import app.admin.models  # noqa: F401
import app.auth.models  # noqa: F401
import app.bookings.models  # noqa: F401
import app.notifications.models  # noqa: F401
import app.resources.models  # noqa: F401
import app.waitlist.models  # noqa: F401
from alembic import context
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            from app.config import get_settings

            url = get_settings().DATABASE_URL
        except Exception:
            pass
    if not url:
        url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is required")
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with asyncpg."""
    connectable = config.attributes.get("connection", None)

    if connectable is None:
        url = get_url()
        engine = create_async_engine(
            url,
            poolclass=pool.NullPool,
        )

        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)

        await engine.dispose()
    else:
        if isinstance(connectable, AsyncEngine):
            async with connectable.connect() as connection:
                await connection.run_sync(do_run_migrations)
        elif isinstance(connectable, AsyncConnection):
            await connection.run_sync(do_run_migrations)
        else:
            do_run_migrations(connectable)


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = config.attributes.get("connection", None)
    if connectable is not None and not isinstance(connectable, (AsyncEngine, AsyncConnection)):
        do_run_migrations(connectable)
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, run_async_migrations())
            future.result()
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
