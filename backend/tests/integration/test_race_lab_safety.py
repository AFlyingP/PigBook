"""
backend/tests/integration/test_race_lab_safety.py - Behavioral safety tests for Disposable Race Lab.

Verifies strict safety contracts:
1. Refusal before destructive SQL for:
   - Malformed approval token
   - Wrong prefix / name / run ID
   - Application / production-like database names
   - Unapproved cleanup action
   - Non-loopback / unknown hosts
2. Proof that only the exact disposable namespace is touched and cleaned up.
3. Proof of before/after outcomes against real PostgreSQL:
   - Unprotected table: 2 active overlapping commits
   - Protected table: 1 commit, 1 SQLSTATE 23P01 (bookings_no_overlap)
"""

import json
import os
import sys
import urllib.parse
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import race_demo  # noqa: E402
from race_demo import (  # noqa: E402
    DISPOSABLE_PREFIX,
    LAB_CONSTRAINT_NAME,
    LAB_SCHEMA,
    RaceLabSafetyError,
    cleanup_disposable_database,
    create_disposable_database,
    normalize_asyncpg_url,
    run_protected_race,
    run_unprotected_race,
    setup_lab_schema,
    validate_approval,
    validate_database_name,
    validate_host,
)


@pytest.fixture
def admin_url() -> str:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.fail("DATABASE_URL environment variable is required for integration tests")
    # Point to postgres maintenance database or default test database
    parsed = urllib.parse.urlsplit(db_url)
    return urllib.parse.urlunsplit(parsed._replace(path="/postgres"))


def create_valid_approval(
    run_id: str,
    host: str = "127.0.0.1",
    actions: list[str] | None = None,
    token: str = "test-approval-token-minimum-16-bytes",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "database": f"{DISPOSABLE_PREFIX}{run_id}",
        "host": host,
        "port": 5432,
        "token": token,
        "actions": actions if actions is not None else ["create", "cleanup"],
    }


# ==============================================================================
# 1. Refusal before destructive SQL: Tokens, Names, Hosts, Actions
# ==============================================================================


@pytest.mark.parametrize(
    "bad_token",
    [
        "",
        "   ",
        "short",
        "only-15-chars--",
        None,
    ],
)
def test_refusal_on_malformed_token(bad_token: str | None) -> None:
    approval = {
        "run_id": "run_01",
        "database": f"{DISPOSABLE_PREFIX}run_01",
        "host": "127.0.0.1",
        "token": bad_token,
        "actions": ["create", "cleanup"],
    }
    with pytest.raises(RaceLabSafetyError, match="token is missing or malformed"):
        validate_approval(approval, required_action="create", expected_run_id="run_01")


def test_refusal_on_token_mismatch() -> None:
    approval = create_valid_approval("run_02", token="valid-approval-token-alpha-123456")
    with pytest.raises(RaceLabSafetyError, match="does not match approval document"):
        validate_approval(
            approval,
            required_action="create",
            expected_run_id="run_02",
            expected_token="valid-approval-token-beta-999999",
        )


@pytest.mark.parametrize(
    "bad_db_name",
    [
        "bad_prefix_123",
        "racelab_test",
        "commonsbook_test_db",
        f"{DISPOSABLE_PREFIX}",  # empty run id
        f"{DISPOSABLE_PREFIX}bad;drop table bookings;--",
        "my_custom_db",
        "",
        None,
    ],
)
def test_refusal_on_wrong_prefix_or_invalid_name(bad_db_name: str | None) -> None:
    with pytest.raises(RaceLabSafetyError):
        validate_database_name(bad_db_name)


def test_refusal_on_run_id_mismatch() -> None:
    approval = create_valid_approval("run_03")
    with pytest.raises(RaceLabSafetyError, match="does not match expected run ID"):
        validate_approval(approval, required_action="create", expected_run_id="run_different")


@pytest.mark.parametrize(
    "prod_name",
    [
        "commonsbook",
        "commonsbook_production",
        "commonsbook_staging",
        "commonsbook_test",
        "postgres",
        "template0",
        "template1",
        "app_production_db",
        f"{DISPOSABLE_PREFIX}production",
        f"{DISPOSABLE_PREFIX}staging_01",
    ],
)
def test_refusal_on_application_and_production_names(prod_name: str) -> None:
    with pytest.raises(RaceLabSafetyError):
        validate_database_name(prod_name)


@pytest.mark.parametrize(
    "bad_host",
    [
        "remote.production.internal",
        "192.168.1.100",
        "10.0.0.1",
        "8.8.8.8",
        "db.mycloudprovider.com",
        "aws.rds.example.com",
        "host.docker.internal",
        "cb_test_container",
        "commonsbook_test_db",
        "test-container-node-1",
        "",
        None,
    ],
)
def test_refusal_on_non_loopback_and_unknown_hosts(bad_host: str | None) -> None:
    with pytest.raises(RaceLabSafetyError, match="Refusing host|cannot be empty"):
        validate_host(bad_host)


@pytest.mark.parametrize(
    "good_host",
    [
        "127.0.0.1",
        "localhost",
        "::1",
        "127.0.0.42",
    ],
)
def test_acceptance_of_loopback_hosts(good_host: str) -> None:
    validate_host(good_host)


def test_refusal_on_unapproved_cleanup() -> None:
    run_id = f"test_no_clean_{uuid.uuid4().hex[:8]}"
    approval = create_valid_approval(run_id, actions=["create"])  # Only create, NO cleanup

    with pytest.raises(RaceLabSafetyError, match="Action 'cleanup' is not authorized"):
        validate_approval(approval, required_action="cleanup", expected_run_id=run_id)


# ==============================================================================
# 2. Refusal Before Any Destructive SQL / Database Connection
# ==============================================================================


@pytest.mark.asyncio
async def test_create_refuses_before_connect_on_malformed_token() -> None:
    run_id = "run_token_mock_test"
    db_name = f"{DISPOSABLE_PREFIX}{run_id}"
    approval = {
        "run_id": run_id,
        "database": db_name,
        "host": "127.0.0.1",
        "token": "too-short",
        "actions": ["create"],
    }
    with patch("race_demo.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
        with pytest.raises(RaceLabSafetyError, match="token is missing or malformed"):
            await create_disposable_database(
                admin_dsn="postgresql://user:pass@127.0.0.1:5432/postgres",
                db_name=db_name,
                run_id=run_id,
                approval_data=approval,
            )
        mock_connect.assert_not_called()

        with pytest.raises(RaceLabSafetyError, match="token is missing or malformed"):
            await cleanup_disposable_database(
                admin_dsn="postgresql://user:pass@127.0.0.1:5432/postgres",
                db_name=db_name,
                run_id=run_id,
                approval_data=approval,
            )
        mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_create_refuses_before_connect_on_unauthorized_host() -> None:
    run_id = "run_host_mock_test"
    db_name = f"{DISPOSABLE_PREFIX}{run_id}"
    approval = create_valid_approval(run_id, host="127.0.0.1")

    for unauth_dsn in [
        "postgresql://user:pass@remote.production.internal:5432/postgres",
        "postgresql://user:pass@host.docker.internal:5432/postgres",
        "postgresql://user:pass@test-container-db:5432/postgres",
    ]:
        with patch("race_demo.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
            with pytest.raises(RaceLabSafetyError, match="Refusing host"):
                await create_disposable_database(
                    admin_dsn=unauth_dsn,
                    db_name=db_name,
                    run_id=run_id,
                    approval_data=approval,
                )
            mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_create_refuses_before_connect_on_invalid_db_name() -> None:
    run_id = "run_name_mock_test"
    approval = create_valid_approval(run_id)

    with patch("race_demo.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
        with pytest.raises(RaceLabSafetyError, match="Refusing protected database name"):
            await create_disposable_database(
                admin_dsn="postgresql://user:pass@127.0.0.1:5432/postgres",
                db_name="commonsbook_production",
                run_id=run_id,
                approval_data=approval,
            )
        mock_connect.assert_not_called()

        with pytest.raises(RaceLabSafetyError, match="Refusing protected database name"):
            await cleanup_disposable_database(
                admin_dsn="postgresql://user:pass@127.0.0.1:5432/postgres",
                db_name="commonsbook_production",
                run_id=run_id,
                approval_data=approval,
            )
        mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_refuses_before_connect_on_unapproved_action() -> None:
    run_id = "run_clean_mock_test"
    db_name = f"{DISPOSABLE_PREFIX}{run_id}"
    approval = create_valid_approval(run_id, actions=["create"])  # Missing cleanup

    with patch("race_demo.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
        with pytest.raises(RaceLabSafetyError, match="Action 'cleanup' is not authorized"):
            await cleanup_disposable_database(
                admin_dsn="postgresql://user:pass@127.0.0.1:5432/postgres",
                db_name=db_name,
                run_id=run_id,
                approval_data=approval,
            )
        mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_refuses_before_connect_on_bad_host() -> None:
    run_id = "run_clean_bad_host"
    db_name = f"{DISPOSABLE_PREFIX}{run_id}"
    approval = create_valid_approval(run_id, host="127.0.0.1")

    with patch("race_demo.asyncpg.connect", new_callable=AsyncMock) as mock_connect:
        with pytest.raises(RaceLabSafetyError, match="Refusing host"):
            await cleanup_disposable_database(
                admin_dsn="postgresql://user:pass@aws.rds.example.com:5432/postgres",
                db_name=db_name,
                run_id=run_id,
                approval_data=approval,
            )
        mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_create_refuses_destructive_reset_when_db_exists_without_reset_action() -> None:
    run_id = "run_exists_no_reset"
    db_name = f"{DISPOSABLE_PREFIX}{run_id}"
    approval = create_valid_approval(run_id, actions=["create", "cleanup"])

    mock_conn = AsyncMock()
    # Mock pg_database check: returns 1 meaning database already exists
    mock_conn.fetchval = AsyncMock(return_value=1)
    mock_conn.execute = AsyncMock()
    mock_conn.close = AsyncMock()

    with patch("race_demo.asyncpg.connect", new_callable=AsyncMock, return_value=mock_conn):
        with pytest.raises(RaceLabSafetyError, match="already exists. Refusing destructive reset"):
            await create_disposable_database(
                admin_dsn="postgresql://user:pass@127.0.0.1:5432/postgres",
                db_name=db_name,
                run_id=run_id,
                approval_data=approval,
            )

        # Crucial invariant: DROP DATABASE must NOT have been called
        executed_sqls = [
            call_args[0][0] for call_args in mock_conn.execute.call_args_list if call_args[0]
        ]
        for sql in executed_sqls:
            assert "DROP DATABASE" not in sql, (
                f"Destructive DROP DATABASE executed unexpectedly: {sql}"
            )
        assert mock_conn.close.called


@pytest.mark.asyncio
async def test_create_allows_reset_when_explicitly_authorized() -> None:
    run_id = "run_exists_with_reset"
    db_name = f"{DISPOSABLE_PREFIX}{run_id}"
    # Explicitly authorizes reset
    approval = create_valid_approval(run_id, actions=["create", "reset", "cleanup"])

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=1)
    mock_conn.execute = AsyncMock()
    mock_conn.close = AsyncMock()

    with patch("race_demo.asyncpg.connect", new_callable=AsyncMock, return_value=mock_conn):
        await create_disposable_database(
            admin_dsn="postgresql://user:pass@127.0.0.1:5432/postgres",
            db_name=db_name,
            run_id=run_id,
            approval_data=approval,
        )

        executed_sqls = [
            call_args[0][0] for call_args in mock_conn.execute.call_args_list if call_args[0]
        ]
        drop_calls = [s for s in executed_sqls if "DROP DATABASE" in s]
        create_calls = [s for s in executed_sqls if "CREATE DATABASE" in s]
        assert len(drop_calls) == 1, (
            "Expected DROP DATABASE to be executed when reset is authorized"
        )
        assert len(create_calls) == 1, "Expected CREATE DATABASE to be executed"
        assert mock_conn.close.called


# ==============================================================================
# 2. Namespace Isolation: Only exact disposable DB touched, then verified dropped
# ==============================================================================


@pytest.mark.asyncio
async def test_exact_disposable_namespace_isolation_and_verified_cleanup(admin_url: str) -> None:
    run_id = f"iso_{uuid.uuid4().hex[:8]}"
    target_db = f"{DISPOSABLE_PREFIX}{run_id}"
    token = "test-token-isolation-exact-namespace-12345"
    approval = create_valid_approval(run_id, token=token, actions=["create", "cleanup"])

    # Prove database does not exist initially
    clean_admin = normalize_asyncpg_url(admin_url)
    conn = await asyncpg.connect(clean_admin)
    try:
        initial_exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target_db
        )
        assert not initial_exists, f"Database {target_db} must not exist before test"
    finally:
        await conn.close()

    # Create the disposable database
    await create_disposable_database(
        admin_dsn=admin_url,
        db_name=target_db,
        run_id=run_id,
        approval_data=approval,
        approval_token=token,
    )

    # Verify that the target database was created
    conn = await asyncpg.connect(clean_admin)
    try:
        created_exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target_db
        )
        assert created_exists == 1, f"Database {target_db} must exist after creation"
    finally:
        await conn.close()

    # Connect to target database and set up schema
    parsed_admin = urllib.parse.urlsplit(admin_url)
    target_parsed = parsed_admin._replace(path=f"/{target_db}")
    target_dsn = urllib.parse.urlunsplit(target_parsed)

    await setup_lab_schema(target_dsn)

    # Prove schema and tables exist in disposable database
    conn_target = await asyncpg.connect(normalize_asyncpg_url(target_dsn))
    try:
        schema_exists = await conn_target.fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", LAB_SCHEMA
        )
        assert schema_exists == 1, f"Schema {LAB_SCHEMA} must exist in {target_db}"

        tables = await conn_target.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = $1", LAB_SCHEMA
        )
        table_names = {r["table_name"] for r in tables}
        assert "bookings_unprotected" in table_names
        assert "bookings_protected" in table_names
    finally:
        await conn_target.close()

    # Prove schema does NOT exist in admin / maintenance database
    conn_admin = await asyncpg.connect(clean_admin)
    try:
        admin_schema_exists = await conn_admin.fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", LAB_SCHEMA
        )
        assert not admin_schema_exists, f"Schema {LAB_SCHEMA} must NOT touch admin/system databases"
    finally:
        await conn_admin.close()

    # Perform verified cleanup
    cleanup_info = await cleanup_disposable_database(
        admin_dsn=admin_url,
        db_name=target_db,
        run_id=run_id,
        approval_data=approval,
        approval_token=token,
    )
    assert cleanup_info["dropped"] is True
    assert cleanup_info["verified_nonexistent"] is True

    # Confirm it is no longer present in pg_database
    conn_check = await asyncpg.connect(clean_admin)
    try:
        after_exists = await conn_check.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target_db
        )
        assert not after_exists, f"Database {target_db} must not exist after cleanup"
    finally:
        await conn_check.close()


@pytest.mark.asyncio
async def test_real_postgres_refuses_existing_db_without_reset_authorization(
    admin_url: str,
) -> None:
    run_id = f"real_exist_{uuid.uuid4().hex[:8]}"
    target_db = f"{DISPOSABLE_PREFIX}{run_id}"
    token = "test-real-existing-token-16-chars"
    approval_no_reset = create_valid_approval(run_id, token=token, actions=["create", "cleanup"])
    approval_with_reset = create_valid_approval(
        run_id, token=token, actions=["create", "reset", "cleanup"]
    )

    # 1. Initial creation succeeds
    await create_disposable_database(
        admin_dsn=admin_url,
        db_name=target_db,
        run_id=run_id,
        approval_data=approval_no_reset,
        approval_token=token,
    )

    try:
        # 2. Second creation WITHOUT reset authorization must fail and refuse drop
        with pytest.raises(RaceLabSafetyError, match="already exists. Refusing destructive reset"):
            await create_disposable_database(
                admin_dsn=admin_url,
                db_name=target_db,
                run_id=run_id,
                approval_data=approval_no_reset,
                approval_token=token,
            )

        # 3. Creation WITH reset authorization succeeds
        await create_disposable_database(
            admin_dsn=admin_url,
            db_name=target_db,
            run_id=run_id,
            approval_data=approval_with_reset,
            approval_token=token,
        )
    finally:
        # Clean up
        await cleanup_disposable_database(
            admin_dsn=admin_url,
            db_name=target_db,
            run_id=run_id,
            approval_data=approval_with_reset,
            approval_token=token,
        )


# ==============================================================================
# 3. Proof of Before / After Outcomes Against Real PostgreSQL
# ==============================================================================


@pytest.mark.asyncio
async def test_unprotected_race_creates_two_active_overlaps(admin_url: str) -> None:
    run_id = f"unprot_{uuid.uuid4().hex[:8]}"
    target_db = f"{DISPOSABLE_PREFIX}{run_id}"
    approval = create_valid_approval(run_id)

    await create_disposable_database(
        admin_dsn=admin_url,
        db_name=target_db,
        run_id=run_id,
        approval_data=approval,
    )

    parsed = urllib.parse.urlsplit(admin_url)
    target_dsn = urllib.parse.urlunsplit(parsed._replace(path=f"/{target_db}"))

    try:
        await setup_lab_schema(target_dsn)

        resource_id = uuid.uuid4()
        time_range = "[2026-11-01 09:00:00+00, 2026-11-01 10:00:00+00)"

        result = await run_unprotected_race(
            target_dsn=target_dsn,
            resource_id=resource_id,
            time_range_str=time_range,
        )

        assert result["active_committed_rows_count"] == 2
        assert len(result["overlapping_pairs"]) >= 1
        assert result["outcome"] == "two_active_overlaps_proven"

        writers = result["writers"]
        assert len(writers) == 2
        for w in writers:
            assert w["observed_overlaps_before_insert"] == 0
            assert w["status"] == "committed"

    finally:
        await cleanup_disposable_database(
            admin_dsn=admin_url,
            db_name=target_db,
            run_id=run_id,
            approval_data=approval,
        )


@pytest.mark.asyncio
async def test_protected_race_produces_one_commit_and_one_23P01(admin_url: str) -> None:
    run_id = f"prot_{uuid.uuid4().hex[:8]}"
    target_db = f"{DISPOSABLE_PREFIX}{run_id}"
    approval = create_valid_approval(run_id)

    await create_disposable_database(
        admin_dsn=admin_url,
        db_name=target_db,
        run_id=run_id,
        approval_data=approval,
    )

    parsed = urllib.parse.urlsplit(admin_url)
    target_dsn = urllib.parse.urlunsplit(parsed._replace(path=f"/{target_db}"))

    try:
        await setup_lab_schema(target_dsn)

        resource_id = uuid.uuid4()
        time_range = "[2026-11-01 09:00:00+00, 2026-11-01 10:00:00+00)"

        result = await run_protected_race(
            target_dsn=target_dsn,
            resource_id=resource_id,
            time_range_str=time_range,
        )

        assert result["active_committed_rows_count"] == 1
        assert result["lab_constraint_name"] == LAB_CONSTRAINT_NAME
        assert result["outcome"] == "one_commit_one_23P01_proven"

        writers = result["writers"]
        assert len(writers) == 2

        statuses = {w["status"] for w in writers}
        assert statuses == {"committed", "rejected"}

        rejected = next(w for w in writers if w["status"] == "rejected")
        assert rejected["sqlstate"] == "23P01"
        assert rejected["constraint_name"] == LAB_CONSTRAINT_NAME

    finally:
        await cleanup_disposable_database(
            admin_dsn=admin_url,
            db_name=target_db,
            run_id=run_id,
            approval_data=approval,
        )


# ==============================================================================
# 4. End-to-End Execution and Evidence Archiving
# ==============================================================================


@pytest.mark.asyncio
async def test_end_to_end_race_lab_workflow_and_evidence(admin_url: str, tmp_path: Path) -> None:
    run_id = f"e2e_{uuid.uuid4().hex[:8]}"
    token = "test-e2e-token-minimum-16-bytes-12345"
    approval_file = tmp_path / "approval.json"
    evidence_dir = tmp_path / "evidence"

    approval_data = create_valid_approval(run_id, token=token)
    approval_file.write_text(json.dumps(approval_data), encoding="utf-8")

    result = await race_demo.async_main_logic(
        admin_url=admin_url,
        run_id=run_id,
        approval_file=approval_file,
        approval_token=token,
        evidence_dir=evidence_dir,
        cleanup=True,
    )

    assert result["status"] == "success"
    assert result["before"]["active_committed_rows_count"] == 2
    assert result["after"]["active_committed_rows_count"] == 1
    assert result["cleanup"]["verified_nonexistent"] is True

    # Validate archived files
    res_file = evidence_dir / "race_lab_result.json"
    sql_file = evidence_dir / "race_lab_queries.sql"
    summary_file = evidence_dir / "race_lab_summary.txt"

    assert res_file.is_file()
    assert sql_file.is_file()
    assert summary_file.is_file()

    # Validate json content
    data = json.loads(res_file.read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    assert data["label"] == "disposable_postgresql_race_lab"
    assert data["database"] == f"{DISPOSABLE_PREFIX}{run_id}"
    assert data["lab_constraint_name"] == LAB_CONSTRAINT_NAME
    assert data["cleanup"]["verified_nonexistent"] is True

    # Assert no plaintext passwords exist in any evidence artifact
    parsed_admin = urllib.parse.urlsplit(admin_url)
    if parsed_admin.password:
        assert parsed_admin.password not in res_file.read_text(encoding="utf-8")
        assert parsed_admin.password not in sql_file.read_text(encoding="utf-8")
        assert parsed_admin.password not in summary_file.read_text(encoding="utf-8")

    # Verify that all files in evidence directory strictly exclude raw approval token and passwords
    evidence_files = [p for p in evidence_dir.rglob("*") if p.is_file()]
    assert len(evidence_files) >= 3, "Expected evidence artifacts to be produced"
    for ef in evidence_files:
        content = ef.read_text(encoding="utf-8", errors="ignore")
        assert token not in content, f"Raw approval token found in evidence file: {ef.name}"
        if parsed_admin.password:
            assert parsed_admin.password not in content, (
                f"Database password found in evidence file: {ef.name}"
            )


def test_hash_only_approval_authorization_and_rejection() -> None:
    """Validate that hash-only approval records authorize execution with matching token and reject
    mismatches.
    """
    import hashlib

    run_id = f"hash_test_{uuid.uuid4().hex[:8]}"
    db_name = f"{DISPOSABLE_PREFIX}{run_id}"
    token = "secret-approval-token-alpha-12345"
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    # Hash-only approval record (as persisted in evidence)
    hash_approval = {
        "run_id": run_id,
        "database": db_name,
        "host": "127.0.0.1",
        "port": 5432,
        "token_sha256": token_hash,
        "actions": ["create", "cleanup"],
    }

    # 1. Matching token succeeds
    validate_approval(
        approval_data=hash_approval,
        required_action="create",
        expected_run_id=run_id,
        expected_db=db_name,
        expected_host="127.0.0.1",
        expected_token=token,
    )

    # 2. Mismatched token rejected
    with pytest.raises(RaceLabSafetyError, match="does not match approval document"):
        validate_approval(
            approval_data=hash_approval,
            required_action="create",
            expected_run_id=run_id,
            expected_db=db_name,
            expected_host="127.0.0.1",
            expected_token="secret-approval-token-wrong-99999",
        )

    # 3. Short / empty token rejected
    with pytest.raises(RaceLabSafetyError, match="token is missing or malformed"):
        validate_approval(
            approval_data=hash_approval,
            required_action="create",
            expected_run_id=run_id,
            expected_db=db_name,
            expected_host="127.0.0.1",
            expected_token="short",
        )

    # 4. Missing token rejected
    with pytest.raises(RaceLabSafetyError, match="token is missing or malformed"):
        validate_approval(
            approval_data=hash_approval,
            required_action="create",
            expected_run_id=run_id,
            expected_db=db_name,
            expected_host="127.0.0.1",
            expected_token=None,
        )


@pytest.mark.asyncio
async def test_evidence_directory_persists_only_hash_and_no_raw_tokens_or_passwords(
    admin_url: str, tmp_path: Path
) -> None:
    """Verify that evidence directory contains only hash-only approval record and zero secret
    material.
    """
    import hashlib

    run_id = f"sec_{uuid.uuid4().hex[:8]}"
    raw_token = "ultra-secret-token-value-never-persisted"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    # Temporary approval file OUTSIDE evidence directory
    temp_approval_dir = tmp_path / "temp_approval_outside"
    temp_approval_dir.mkdir(parents=True, exist_ok=True)
    temp_approval_file = temp_approval_dir / "approval.json"
    temp_approval_data = create_valid_approval(run_id, token=raw_token)
    temp_approval_file.write_text(json.dumps(temp_approval_data), encoding="utf-8")

    evidence_dir = tmp_path / "evidence_output"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # Persist hash-only approval record in evidence (never raw token)
    evidence_approval_file = evidence_dir / "approval.json"
    evidence_approval_file.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "database": f"{DISPOSABLE_PREFIX}{run_id}",
                "host": "127.0.0.1",
                "port": 5432,
                "token_sha256": token_hash,
                "actions": ["create", "cleanup"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = await race_demo.async_main_logic(
        admin_url=admin_url,
        run_id=run_id,
        approval_file=temp_approval_file,
        approval_token=raw_token,
        evidence_dir=evidence_dir,
        cleanup=True,
    )
    assert result["status"] == "success"

    # Simulate best-effort cleanup of temporary approval file outside evidence
    temp_approval_file.unlink(missing_ok=True)

    # Assert evidence approval file contains token_sha256 and NOT raw token
    ev_approval_data = json.loads(evidence_approval_file.read_text(encoding="utf-8"))
    assert "token" not in ev_approval_data
    assert ev_approval_data["token_sha256"] == token_hash

    # Search all files in evidence directory: assert raw token and DB password are completely absent
    parsed_admin = urllib.parse.urlsplit(admin_url)
    all_files = list(evidence_dir.rglob("*"))
    assert len(all_files) >= 4  # approval.json, queries.sql, result.json, summary.txt
    for f in all_files:
        if f.is_file():
            content = f.read_text(encoding="utf-8", errors="ignore")
            assert raw_token not in content, f"Raw token leaked into {f.name}"
            if parsed_admin.password:
                assert parsed_admin.password not in content, f"DB password leaked into {f.name}"
