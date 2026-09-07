import os
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

test_metadata = MetaData()

t002_probe = Table(
    "t002_probe",
    test_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(50), nullable=False),
    Column("value", String(100), nullable=False),
)


@pytest.fixture(scope="session")
def db_engine() -> AsyncEngine:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is required for integration tests")

    if "sqlite" in database_url.lower():
        raise RuntimeError("SQLite is strictly forbidden; tests require real PostgreSQL 16")

    return create_async_engine(database_url, poolclass=NullPool, echo=False)


@pytest.fixture
async def db_connection(db_engine: AsyncEngine) -> AsyncGenerator[AsyncConnection, None]:
    async with db_engine.connect() as conn:
        dialect_name = conn.dialect.name
        if dialect_name != "postgresql":
            raise RuntimeError(f"Expected postgresql dialect, got '{dialect_name}'")

        result = await conn.execute(text("SHOW server_version_num"))
        version_num_str = result.scalar_one()
        version_num = int(str(version_num_str).strip())
        major_version = version_num // 10000
        if major_version != 16:
            raise RuntimeError(
                f"PostgreSQL 16 is required. Detected version {version_num} (major {major_version})"
            )

        await conn.run_sync(test_metadata.create_all)
        await conn.commit()

        trans = await conn.begin()
        yield conn
        await trans.rollback()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(
        bind=db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    async with session_factory() as session:
        yield session


@pytest.fixture(scope="session", autouse=True)
def migrated_schema(request: pytest.FixtureRequest) -> None:
    """Migrate once per isolated test process; lifecycle tests still exercise down/up."""
    if any(
        "integration" in item.path.parts or "concurrency" in item.path.parts
        for item in request.session.items
    ):
        from alembic.config import Config

        from alembic import command

        command.upgrade(Config("backend/alembic.ini"), "head")
