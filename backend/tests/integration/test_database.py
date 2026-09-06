from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.conftest import t002_probe
from tests.factories import ProbeRecordFactory

ISOLATION_CHECK_PROBE_NAME = "isolation_check_probe_unique_name"


async def test_database_connectivity(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("SELECT 1"))
    assert result.scalar() == 1


async def test_database_server_major_version_is_16(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("SHOW server_version_num"))
    version_num = int(str(result.scalar_one()).strip())
    major_version = version_num // 10000
    assert major_version == 16, f"Expected PostgreSQL major version 16, got {major_version}"


async def test_database_dialect_is_postgresql_not_sqlite(db_engine: AsyncEngine) -> None:
    assert db_engine.dialect.name == "postgresql"
    assert db_engine.dialect.name != "sqlite"


async def test_rollback_isolation_step_1_write(db_session: AsyncSession) -> None:
    record = ProbeRecordFactory(name=ISOLATION_CHECK_PROBE_NAME, value="ephemeral_probe_value")
    await db_session.execute(insert(t002_probe).values(name=record.name, value=record.value))
    await db_session.commit()

    query = await db_session.execute(
        select(t002_probe).where(t002_probe.c.name == ISOLATION_CHECK_PROBE_NAME)
    )
    row = query.first()
    assert row is not None
    assert row.name == ISOLATION_CHECK_PROBE_NAME


async def test_rollback_isolation_step_2_verify_gone(db_session: AsyncSession) -> None:
    query = await db_session.execute(
        select(t002_probe).where(t002_probe.c.name == ISOLATION_CHECK_PROBE_NAME)
    )
    row = query.first()
    assert row is None, "Row written in previous test was not rolled back; isolation failure!"
