#!/usr/bin/env python3
"""
scripts/race_demo.py - Disposable PostgreSQL Race Lab Demonstration

Demonstrates check-then-insert concurrency vulnerability on unprotected booking table
and verifies invariant enforcement via PostgreSQL GiST exclusion constraint (23P01).

Strict Safety Invariants:
1. No production DSN fallback: requires explicit admin connection input.
2. Refuse non-loopback hosts except positively identified test containers.
3. Refuse application/production database names; exact prefix commonsbook_racelab_<runid>.
4. Validate approval token/file separately authorizing creation and cleanup.
5. Fail closed if database identity, host, or approval changes.
6. Verify and archive disposable database cleanup.
"""

import argparse
import asyncio
import datetime
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import asyncpg

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DATABASE_NAMES = {
    "commonsbook",
    "commonsbook_production",
    "commonsbook_staging",
    "commonsbook_test",
    "postgres",
    "template0",
    "template1",
}

DISPOSABLE_PREFIX = "commonsbook_racelab_"
LAB_SCHEMA = "race_lab"
LAB_CONSTRAINT_NAME = "bookings_no_overlap"


class RaceLabSafetyError(Exception):
    """Raised when any race lab safety contract or invariant check fails."""


def sanitize_dsn(dsn: str) -> str:
    """Mask credentials in connection string for safe logging and evidence."""
    try:
        parsed = urllib.parse.urlsplit(dsn)
        if parsed.password:
            netloc = f"{parsed.username or ''}:***@{parsed.hostname or ''}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urllib.parse.urlunsplit(
                (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )
        return dsn
    except Exception:
        return "<sanitized-connection-string>"


def normalize_asyncpg_url(url: str) -> str:
    """Normalize SQLAlchemy or psycopg DSNs to standard postgresql:// for asyncpg."""
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def validate_host(host: str | None) -> None:
    """
    Validate that the host is loopback.
    Refuse any non-loopback, public, external, container DNS, or remote hosts.
    """
    if not host:
        raise RaceLabSafetyError("Connection host cannot be empty.")

    host_lower = host.strip().lower()

    # Direct loopback hostnames
    if host_lower in {"localhost", "127.0.0.1", "::1"}:
        return

    # Check IP loopback range (127.0.0.0/8 or ::1)
    try:
        ip = ipaddress.ip_address(host_lower)
        if ip.is_loopback:
            return
    except ValueError:
        pass

    raise RaceLabSafetyError(
        f"Refusing host '{host}': must be loopback (127.0.0.1, localhost) or loopback IP. "
        "External, container DNS, and production hosts without verifiable identity "
        "are strictly forbidden."
    )


def validate_database_name(
    db_name: str | None, expected_run_id: str | None = None
) -> None:
    """
    Ensure the target database name strictly adheres to the disposable namespace
    'commonsbook_racelab_<runid>' and never matches production/application names.
    """
    if not db_name:
        raise RaceLabSafetyError("Database name cannot be empty.")

    name = db_name.strip()

    # Reject known application or system databases
    if name.lower() in EXCLUDED_DATABASE_NAMES:
        raise RaceLabSafetyError(
            f"Refusing protected database name '{name}'. Application, system, and "
            f"production databases cannot be used as disposable race lab targets."
        )

    # Reject production/staging keywords
    for keyword in ("production", "prod", "staging"):
        if keyword in name.lower():
            raise RaceLabSafetyError(
                f"Refusing database name '{name}' containing production-like term '{keyword}'."
            )

    # Strict prefix and character pattern
    if not name.startswith(DISPOSABLE_PREFIX):
        raise RaceLabSafetyError(
            f"Database name '{name}' does not start with exact prefix '{DISPOSABLE_PREFIX}'."
        )

    run_id_part = name[len(DISPOSABLE_PREFIX) :]
    if not run_id_part or not re.fullmatch(r"[a-zA-Z0-9_-]+", run_id_part):
        raise RaceLabSafetyError(
            f"Database name '{name}' has invalid run ID part '{run_id_part}'. "
            f"Must be non-empty alphanumeric with optional underscores or hyphens."
        )

    if expected_run_id and run_id_part != expected_run_id:
        raise RaceLabSafetyError(
            f"Database name '{name}' does not match expected run ID '{expected_run_id}'."
        )


def validate_approval(
    approval_data: dict[str, Any],
    required_action: str,
    expected_run_id: str | None = None,
    expected_db: str | None = None,
    expected_host: str | None = None,
    expected_token: str | None = None,
) -> None:
    """
    Validate that an explicit approval document authorizes the requested action.
    Validates token or token_sha256, run ID, exact database name, host, and granular action
    permissions.
    """
    if not isinstance(approval_data, dict):
        raise RaceLabSafetyError("Approval data must be a valid JSON dictionary.")

    token = approval_data.get("token")
    token_sha256 = approval_data.get("token_sha256")

    if token is not None:
        if not isinstance(token, str) or len(token.strip()) < 16:
            raise RaceLabSafetyError(
                "Approval token is missing or malformed (must be >= 16 characters)."
            )
        if expected_token and token.strip() != expected_token.strip():
            raise RaceLabSafetyError(
                "Provided approval token does not match approval document."
            )
        if token_sha256:
            actual_hash = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
            if actual_hash != token_sha256:
                raise RaceLabSafetyError(
                    "Approval token hash does not match approval document."
                )
    elif token_sha256 is not None:
        if not expected_token or len(expected_token.strip()) < 16:
            raise RaceLabSafetyError(
                "Approval token is missing or malformed (must be >= 16 characters)."
            )
        expected_hash = hashlib.sha256(
            expected_token.strip().encode("utf-8")
        ).hexdigest()
        if expected_hash != token_sha256:
            raise RaceLabSafetyError(
                "Provided approval token does not match approval document."
            )
    else:
        raise RaceLabSafetyError(
            "Approval token is missing or malformed (must be >= 16 characters)."
        )

    run_id = approval_data.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[a-zA-Z0-9_-]+", run_id.strip()
    ):
        raise RaceLabSafetyError("Approval run_id is missing or invalid.")

    if expected_run_id and run_id.strip() != expected_run_id.strip():
        raise RaceLabSafetyError(
            f"Approval run_id '{run_id}' does not match expected run ID '{expected_run_id}'."
        )

    db_name = approval_data.get("database")
    expected_name = f"{DISPOSABLE_PREFIX}{run_id.strip()}"
    if db_name != expected_name:
        raise RaceLabSafetyError(
            f"Approval database '{db_name}' does not match expected '{expected_name}'."
        )
    validate_database_name(db_name, expected_run_id=run_id.strip())

    if expected_db and db_name != expected_db:
        raise RaceLabSafetyError(
            f"Approval database '{db_name}' does not match target database '{expected_db}'."
        )

    host = approval_data.get("host")
    validate_host(host)
    if expected_host:
        norm_expected = expected_host.lower()
        norm_actual = host.lower() if host else ""
        if norm_actual != norm_expected and not (
            norm_expected in {"localhost", "127.0.0.1"}
            and norm_actual in {"localhost", "127.0.0.1"}
        ):
            raise RaceLabSafetyError(
                f"Approval host '{host}' does not match connection host '{expected_host}'."
            )

    actions = approval_data.get("actions")
    if not isinstance(actions, list):
        raise RaceLabSafetyError(
            "Approval 'actions' must be a list of authorized action strings."
        )

    if required_action not in actions:
        raise RaceLabSafetyError(
            f"Action '{required_action}' is not authorized in approval file. Authorized: {actions}"
        )


def load_approval_file(file_path: Path | str) -> dict[str, Any]:
    path = Path(file_path)
    if not path.is_file():
        raise RaceLabSafetyError(f"Approval file not found: '{path}'")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Root element is not a dict")
        return data
    except Exception as exc:
        raise RaceLabSafetyError(f"Malformed approval file '{path}': {exc}")


async def create_disposable_database(
    admin_dsn: str,
    db_name: str,
    run_id: str,
    approval_data: dict[str, Any],
    approval_token: str | None = None,
) -> None:
    """Create the dedicated disposable database after verifying safety and approval."""
    validate_database_name(db_name, expected_run_id=run_id)
    parsed = urllib.parse.urlsplit(admin_dsn)
    validate_host(parsed.hostname)
    validate_approval(
        approval_data=approval_data,
        required_action="create",
        expected_run_id=run_id,
        expected_db=db_name,
        expected_host=parsed.hostname,
        expected_token=approval_token,
    )

    clean_admin_dsn = normalize_asyncpg_url(admin_dsn)
    conn = await asyncpg.connect(clean_admin_dsn)
    try:
        # Check if already exists; refuse destructive reset under only create action
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        if exists:
            actions = approval_data.get("actions", [])
            if "reset" not in actions:
                raise RaceLabSafetyError(
                    f"Disposable database '{db_name}' already exists. Refusing destructive reset "
                    "under create action; explicit 'reset' action required to drop "
                    "existing database."
                )
            validate_approval(
                approval_data=approval_data,
                required_action="reset",
                expected_run_id=run_id,
                expected_db=db_name,
                expected_host=parsed.hostname,
                expected_token=approval_token,
            )
            # Terminate connections and drop
            await conn.execute(f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{db_name}' AND pid <> pg_backend_pid()
            """)
            await conn.execute(f'DROP DATABASE "{db_name}"')

        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


async def setup_lab_schema(target_dsn: str) -> None:
    """Set up the isolated race_lab schema and both test tables."""
    clean_dsn = normalize_asyncpg_url(target_dsn)
    conn = await asyncpg.connect(clean_dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {LAB_SCHEMA}")

        # Unprotected table: has status check and range sanity check, but NO exclusion constraint
        await conn.execute(f"""
            CREATE TABLE {LAB_SCHEMA}.bookings_unprotected (
                id uuid PRIMARY KEY,
                resource_id uuid NOT NULL,
                time_range tstzrange NOT NULL,
                status text NOT NULL CHECK(status IN ('confirmed','offered')),
                created_at timestamptz NOT NULL DEFAULT now(),
                CHECK(
                    NOT isempty(time_range) AND NOT lower_inf(time_range)
                    AND NOT upper_inf(time_range) AND lower_inc(time_range)
                    AND NOT upper_inc(time_range) AND isfinite(lower(time_range))
                    AND isfinite(upper(time_range))
                )
            )
        """)

        # Protected table: includes exact GiST exclusion invariant definition
        await conn.execute(f"""
            CREATE TABLE {LAB_SCHEMA}.bookings_protected (
                id uuid PRIMARY KEY,
                resource_id uuid NOT NULL,
                time_range tstzrange NOT NULL,
                status text NOT NULL CHECK(status IN ('confirmed','offered')),
                created_at timestamptz NOT NULL DEFAULT now(),
                CHECK(
                    NOT isempty(time_range) AND NOT lower_inf(time_range)
                    AND NOT upper_inf(time_range) AND lower_inc(time_range)
                    AND NOT upper_inc(time_range) AND isfinite(lower(time_range))
                    AND isfinite(upper(time_range))
                ),
                CONSTRAINT {LAB_CONSTRAINT_NAME} EXCLUDE USING gist (
                    resource_id WITH =,
                    time_range WITH &&
                ) WHERE (status IN ('confirmed','offered'))
            )
        """)
    finally:
        await conn.close()


async def run_unprotected_race(
    target_dsn: str,
    resource_id: uuid.UUID,
    time_range_str: str,
) -> dict[str, Any]:
    """
    Execute two concurrent transactions against race_lab.bookings_unprotected.
    Both perform application check-then-insert synchronized with a barrier.
    Both observe 0 overlaps, then both insert and commit.
    Asserts and archives proof of 2 active overlapping committed rows.
    """
    barrier = asyncio.Barrier(2)
    clean_dsn = normalize_asyncpg_url(target_dsn)

    writer1_id = uuid.uuid4()
    writer2_id = uuid.uuid4()

    async def writer(writer_num: int, booking_id: uuid.UUID) -> dict[str, Any]:
        conn = await asyncpg.connect(clean_dsn)
        try:
            tr = conn.transaction()
            await tr.start()
            try:
                # 1. Application check: inspect for existing active overlap
                query_check = f"""
                    SELECT count(*) FROM {LAB_SCHEMA}.bookings_unprotected
                    WHERE resource_id = $1 AND time_range && $2::text::tstzrange
                      AND status IN ('confirmed', 'offered')
                """
                count = await conn.fetchval(query_check, resource_id, time_range_str)

                # Synchronize both writers: both must observe count == 0 before inserting
                await barrier.wait()

                if writer_num == 2:
                    await asyncio.sleep(0.02)

                # 2. Both writers insert
                insert_sql = f"""
                    INSERT INTO {LAB_SCHEMA}.bookings_unprotected
                    (id, resource_id, time_range, status)
                    VALUES ($1, $2, $3::text::tstzrange, 'confirmed')
                """
                await conn.execute(insert_sql, booking_id, resource_id, time_range_str)
                await tr.commit()
                return {
                    "writer": writer_num,
                    "booking_id": str(booking_id),
                    "observed_overlaps_before_insert": count,
                    "status": "committed",
                }
            except Exception as e:
                await tr.rollback()
                return {
                    "writer": writer_num,
                    "booking_id": str(booking_id),
                    "status": "failed",
                    "error": str(e),
                }
        finally:
            await conn.close()

    results = await asyncio.gather(
        writer(1, writer1_id),
        writer(2, writer2_id),
    )

    # Verification queries
    conn = await asyncpg.connect(clean_dsn)
    try:
        active_rows = await conn.fetch(
            f"SELECT id, resource_id, time_range::text, status "
            f"FROM {LAB_SCHEMA}.bookings_unprotected "
            f"WHERE resource_id = $1 AND status IN ('confirmed', 'offered')",
            resource_id,
        )

        overlap_pairs = await conn.fetch(
            f"""
            SELECT a.id as id_a, b.id as id_b,
                   a.time_range::text as range_a, b.time_range::text as range_b
            FROM {LAB_SCHEMA}.bookings_unprotected a
            JOIN {LAB_SCHEMA}.bookings_unprotected b
              ON a.id < b.id
             AND a.resource_id = b.resource_id
             AND a.time_range && b.time_range
             AND a.status IN ('confirmed', 'offered')
             AND b.status IN ('confirmed', 'offered')
            WHERE a.resource_id = $1
            """,
            resource_id,
        )
    finally:
        await conn.close()

    if len(active_rows) != 2:
        raise RuntimeError(
            f"Expected 2 committed rows in unprotected table, found {len(active_rows)}"
        )
    if len(overlap_pairs) < 1:
        raise RuntimeError(
            f"Expected at least 1 overlapping pair in unprotected table, found {len(overlap_pairs)}"
        )

    return {
        "table": f"{LAB_SCHEMA}.bookings_unprotected",
        "resource_id": str(resource_id),
        "requested_interval": time_range_str,
        "writers": results,
        "active_committed_rows_count": len(active_rows),
        "active_rows": [
            {
                "id": str(r["id"]),
                "resource_id": str(r["resource_id"]),
                "time_range": r["time_range"],
                "status": r["status"],
            }
            for r in active_rows
        ],
        "overlapping_pairs": [
            {
                "id_a": str(p["id_a"]),
                "id_b": str(p["id_b"]),
                "range_a": p["range_a"],
                "range_b": p["range_b"],
            }
            for p in overlap_pairs
        ],
        "outcome": "two_active_overlaps_proven",
    }


async def run_protected_race(
    target_dsn: str,
    resource_id: uuid.UUID,
    time_range_str: str,
) -> dict[str, Any]:
    """
    Execute two concurrent transactions against race_lab.bookings_protected.
    Both perform application check-then-insert synchronized with a barrier.
    PostgreSQL GiST exclusion constraint ensures exactly one commit and one 23P01 error.
    Asserts and archives invariant enforcement.
    """
    barrier = asyncio.Barrier(2)
    clean_dsn = normalize_asyncpg_url(target_dsn)

    writer1_id = uuid.uuid4()
    writer2_id = uuid.uuid4()

    async def writer(writer_num: int, booking_id: uuid.UUID) -> dict[str, Any]:
        conn = await asyncpg.connect(clean_dsn)
        try:
            tr = conn.transaction()
            await tr.start()
            try:
                # 1. Application check: inspect for existing active overlap
                query_check = f"""
                    SELECT count(*) FROM {LAB_SCHEMA}.bookings_protected
                    WHERE resource_id = $1 AND time_range && $2::text::tstzrange
                      AND status IN ('confirmed', 'offered')
                """
                count = await conn.fetchval(query_check, resource_id, time_range_str)

                # Synchronize both writers
                await barrier.wait()

                if writer_num == 2:
                    await asyncio.sleep(0.05)

                # 2. Both writers attempt insert
                insert_sql = f"""
                    INSERT INTO {LAB_SCHEMA}.bookings_protected
                    (id, resource_id, time_range, status)
                    VALUES ($1, $2, $3::text::tstzrange, 'confirmed')
                """
                await conn.execute(insert_sql, booking_id, resource_id, time_range_str)
                await tr.commit()
                return {
                    "writer": writer_num,
                    "booking_id": str(booking_id),
                    "observed_overlaps_before_insert": count,
                    "status": "committed",
                    "sqlstate": None,
                }
            except asyncpg.exceptions.ExclusionViolationError as e:
                await tr.rollback()
                return {
                    "writer": writer_num,
                    "booking_id": str(booking_id),
                    "observed_overlaps_before_insert": count,
                    "status": "rejected",
                    "sqlstate": e.sqlstate,
                    "constraint_name": e.constraint_name or LAB_CONSTRAINT_NAME,
                    "error_message": str(e),
                }
            except Exception as e:
                await tr.rollback()
                return {
                    "writer": writer_num,
                    "booking_id": str(booking_id),
                    "status": "unexpected_error",
                    "error_message": str(e),
                }
        finally:
            await conn.close()

    results = await asyncio.gather(
        writer(1, writer1_id),
        writer(2, writer2_id),
    )

    # Verification queries
    conn = await asyncpg.connect(clean_dsn)
    try:
        active_rows = await conn.fetch(
            f"SELECT id, resource_id, time_range::text, status "
            f"FROM {LAB_SCHEMA}.bookings_protected "
            f"WHERE resource_id = $1 AND status IN ('confirmed', 'offered')",
            resource_id,
        )
    finally:
        await conn.close()

    statuses = [r["status"] for r in results]
    commits = sum(1 for s in statuses if s == "committed")
    rejections = sum(1 for s in statuses if s == "rejected")
    sqlstates = [r.get("sqlstate") for r in results if r.get("sqlstate")]

    if commits != 1 or rejections != 1:
        raise RuntimeError(
            f"Expected exactly 1 commit and 1 rejection in protected table. "
            f"Observed {commits} commits, {rejections} rejections. Results: {results}"
        )

    if sqlstates != ["23P01"]:
        raise RuntimeError(f"Expected SQLSTATE '23P01', observed: {sqlstates}")

    if len(active_rows) != 1:
        raise RuntimeError(
            f"Expected exactly 1 active row in protected table, found {len(active_rows)}"
        )

    return {
        "table": f"{LAB_SCHEMA}.bookings_protected",
        "lab_constraint_name": LAB_CONSTRAINT_NAME,
        "resource_id": str(resource_id),
        "requested_interval": time_range_str,
        "writers": results,
        "active_committed_rows_count": len(active_rows),
        "active_rows": [
            {
                "id": str(r["id"]),
                "resource_id": str(r["resource_id"]),
                "time_range": r["time_range"],
                "status": r["status"],
            }
            for r in active_rows
        ],
        "outcome": "one_commit_one_23P01_proven",
    }


async def cleanup_disposable_database(
    admin_dsn: str,
    db_name: str,
    run_id: str,
    approval_data: dict[str, Any],
    approval_token: str | None = None,
) -> dict[str, Any]:
    """
    Drop the disposable database strictly after validating separate cleanup approval.
    Verifies that only the positively identified disposable database is dropped,
    and proves it no longer exists.
    """
    validate_database_name(db_name, expected_run_id=run_id)
    parsed = urllib.parse.urlsplit(admin_dsn)
    validate_host(parsed.hostname)
    validate_approval(
        approval_data=approval_data,
        required_action="cleanup",
        expected_run_id=run_id,
        expected_db=db_name,
        expected_host=parsed.hostname,
        expected_token=approval_token,
    )

    clean_admin_dsn = normalize_asyncpg_url(admin_dsn)
    conn = await asyncpg.connect(clean_admin_dsn)
    try:
        # Terminate remaining connections to disposable DB
        await conn.execute(f"""
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = '{db_name}' AND pid <> pg_backend_pid()
        """)
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')

        # Prove removal
        still_exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        if still_exists:
            raise RaceLabSafetyError(
                f"Database '{db_name}' still exists after drop command."
            )
    finally:
        await conn.close()

    return {
        "database": db_name,
        "dropped": True,
        "verified_nonexistent": True,
    }


def get_git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def compute_configuration_hash() -> str:
    hasher = hashlib.sha256()
    for rel in [
        "backend/pyproject.toml",
        "scripts/verification.json",
        "compose.test.yaml",
    ]:
        p = REPO_ROOT / rel
        if p.exists():
            hasher.update(p.read_bytes())
    return hasher.hexdigest()


def write_evidence(
    evidence_dir: Path,
    run_id: str,
    db_name: str,
    host: str,
    before_result: dict[str, Any],
    after_result: dict[str, Any],
    cleanup_result: dict[str, Any] | None,
    command_str: str,
    approval_token_sha256: str | None = None,
) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    git_sha = get_git_sha()
    cfg_hash = compute_configuration_hash()

    # SQL queries artifact
    sql_content = f"""-- Disposable Race Lab SQL Artifacts
-- Run ID: {run_id}
-- Database: {db_name}
-- Timestamp: {timestamp}

-- Schema Creation
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE SCHEMA IF NOT EXISTS {LAB_SCHEMA};

-- Before Scenario: Unprotected Table (Check-then-insert vulnerability)
CREATE TABLE {LAB_SCHEMA}.bookings_unprotected (
    id uuid PRIMARY KEY,
    resource_id uuid NOT NULL,
    time_range tstzrange NOT NULL,
    status text NOT NULL CHECK(status IN ('confirmed','offered')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(NOT isempty(time_range) AND NOT lower_inf(time_range) AND NOT upper_inf(time_range)
          AND lower_inc(time_range) AND NOT upper_inc(time_range)
          AND isfinite(lower(time_range)) AND isfinite(upper(time_range)))
);

-- Two concurrent transactions execute:
-- 1. SELECT count(*) FROM {LAB_SCHEMA}.bookings_unprotected
--    WHERE resource_id = :id AND time_range && :range; (Returns 0)
-- 2. INSERT INTO {LAB_SCHEMA}.bookings_unprotected VALUES (...);
-- Result: Both commit successfully, creating 2 active overlapping rows.

-- After Scenario: Protected Table (PostgreSQL GiST Exclusion Invariant)
CREATE TABLE {LAB_SCHEMA}.bookings_protected (
    id uuid PRIMARY KEY,
    resource_id uuid NOT NULL,
    time_range tstzrange NOT NULL,
    status text NOT NULL CHECK(status IN ('confirmed','offered')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(NOT isempty(time_range) AND NOT lower_inf(time_range) AND NOT upper_inf(time_range)
          AND lower_inc(time_range) AND NOT upper_inc(time_range)
          AND isfinite(lower(time_range)) AND isfinite(upper(time_range))),
    CONSTRAINT {LAB_CONSTRAINT_NAME} EXCLUDE USING gist (
        resource_id WITH =,
        time_range WITH &&
    ) WHERE (status IN ('confirmed','offered'))
);

-- Two concurrent transactions execute identical check-then-insert queries.
-- Writer 1 commits successfully.
-- Writer 2 raises SQLSTATE 23P01 naming constraint '{LAB_CONSTRAINT_NAME}'.
-- Result: Exactly 1 active committed row; exclusion violation strictly enforced.
"""
    (evidence_dir / "race_lab_queries.sql").write_text(sql_content, encoding="utf-8")

    # Machine-readable artifact (persists SHA-256 hash of approval token, never raw token)
    result_data: dict[str, Any] = {
        "synthetic": True,
        "label": "disposable_postgresql_race_lab",
        "timestamp": timestamp,
        "git_sha": git_sha,
        "configuration_hash": cfg_hash,
        "command": command_str,
        "status": "success",
        "run_id": run_id,
        "database": db_name,
        "host": host,
        "lab_schema": LAB_SCHEMA,
        "lab_constraint_name": LAB_CONSTRAINT_NAME,
        "before": before_result,
        "after": after_result,
        "cleanup": cleanup_result or {"authorized": False, "dropped": False},
    }
    if approval_token_sha256:
        result_data["approval_token_sha256"] = approval_token_sha256
    (evidence_dir / "race_lab_result.json").write_text(
        json.dumps(result_data, indent=2), encoding="utf-8"
    )

    # Human-readable summary artifact
    w1_before = before_result["writers"][0]
    w2_before = before_result["writers"][1]
    w1_after = after_result["writers"][0]
    w2_after = after_result["writers"][1]
    cleanup_auth = bool(cleanup_result and cleanup_result.get("dropped", False))
    cleanup_ver = bool(
        cleanup_result and cleanup_result.get("verified_nonexistent", False)
    )

    summary = (
        "CommonsBook Disposable PostgreSQL Race Lab Demonstration\n"
        "===========================================================\n"
        f"Timestamp:          {timestamp}\n"
        f"Git SHA:            {git_sha}\n"
        f"Database:           {db_name}\n"
        f"Host:               {host}\n"
        "Synthetic Label:    synthetic: true\n\n"
        "1. Before Phase (Unprotected check-then-insert):\n"
        f"   - Table: {before_result['table']}\n"
        f"   - Writer 1: {w1_before['status']} "
        f"(observed {w1_before['observed_overlaps_before_insert']} overlaps)\n"
        f"   - Writer 2: {w2_before['status']} "
        f"(observed {w2_before['observed_overlaps_before_insert']} overlaps)\n"
        f"   - Active overlapping rows committed: {before_result['active_committed_rows_count']}\n"
        f"   - Overlapping pairs detected: {len(before_result['overlapping_pairs'])}\n"
        "   - Finding: Vulnerability reproduced. Application preflight checks cannot prevent\n"
        "     double-booking under concurrent execution.\n\n"
        "2. After Phase (Protected with GiST exclusion constraint):\n"
        f"   - Table: {after_result['table']}\n"
        f"   - Constraint: {after_result['lab_constraint_name']}\n"
        f"   - Writer 1: {w1_after['status']}\n"
        f"   - Writer 2: {w2_after['status']} "
        f"(SQLSTATE {w2_after.get('sqlstate')}, "
        f"Constraint: {w2_after.get('constraint_name')})\n"
        f"   - Active committed rows: {after_result['active_committed_rows_count']}\n"
        "   - Finding: Invariant verified. PostgreSQL GiST exclusion constraint deterministically\n"
        "     rejects overlapping interval commit with SQLSTATE 23P01.\n\n"
        "3. Cleanup:\n"
        f"   - Separate Cleanup Authorized: {cleanup_auth}\n"
        f"   - Verified Ephemeral Database Removed: {cleanup_ver}\n"
    )
    (evidence_dir / "race_lab_summary.txt").write_text(summary, encoding="utf-8")


async def async_main_logic(
    admin_url: str,
    run_id: str,
    approval_file: str | Path | None = None,
    approval_data: dict[str, Any] | None = None,
    approval_token: str | None = None,
    evidence_dir: str | Path | None = None,
    cleanup: bool = True,
    command_str: str = "python scripts/race_demo.py",
) -> dict[str, Any]:
    """Core programmatic entry point for disposable race lab."""
    if not admin_url:
        raise RaceLabSafetyError(
            "Explicit admin connection URL is required (no fallback)."
        )

    # Safety checks on admin URL
    parsed_admin = urllib.parse.urlsplit(admin_url)
    validate_host(parsed_admin.hostname)

    # Load approval data
    if approval_data is None:
        if not approval_file:
            raise RaceLabSafetyError(
                "Approval document or approval file path is required."
            )
        approval_data = load_approval_file(approval_file)

    target_db_name = f"{DISPOSABLE_PREFIX}{run_id}"
    validate_database_name(target_db_name, expected_run_id=run_id)

    # 1. Create ephemeral database
    await create_disposable_database(
        admin_dsn=admin_url,
        db_name=target_db_name,
        run_id=run_id,
        approval_data=approval_data,
        approval_token=approval_token,
    )

    # Construct target connection DSN
    # Replace database in admin URL path with target_db_name
    target_parsed = parsed_admin._replace(path=f"/{target_db_name}")
    target_dsn = urllib.parse.urlunsplit(target_parsed)

    # 2. Setup schema and tables
    await setup_lab_schema(target_dsn)

    # 3. Test scenarios with identical booking target
    test_resource_id = uuid.uuid4()
    test_time_range = "[2026-10-15 14:00:00+00, 2026-10-15 15:00:00+00)"

    # Run unprotected race
    before_result = await run_unprotected_race(
        target_dsn=target_dsn,
        resource_id=test_resource_id,
        time_range_str=test_time_range,
    )

    # Run protected race
    after_result = await run_protected_race(
        target_dsn=target_dsn,
        resource_id=test_resource_id,
        time_range_str=test_time_range,
    )

    # 4. Optional / Separate Cleanup
    cleanup_result = None
    if cleanup:
        cleanup_result = await cleanup_disposable_database(
            admin_dsn=admin_url,
            db_name=target_db_name,
            run_id=run_id,
            approval_data=approval_data,
            approval_token=approval_token,
        )

    # 5. Archive evidence
    if evidence_dir:
        token_val = approval_token or (
            approval_data.get("token") if approval_data else None
        )
        token_sha256 = approval_data.get("token_sha256") if approval_data else None
        if not token_sha256 and token_val:
            token_sha256 = hashlib.sha256(token_val.strip().encode("utf-8")).hexdigest()
        write_evidence(
            evidence_dir=Path(evidence_dir),
            run_id=run_id,
            db_name=target_db_name,
            host=parsed_admin.hostname or "127.0.0.1",
            before_result=before_result,
            after_result=after_result,
            cleanup_result=cleanup_result,
            command_str=command_str,
            approval_token_sha256=token_sha256,
        )

    return {
        "status": "success",
        "run_id": run_id,
        "database": target_db_name,
        "before": before_result,
        "after": after_result,
        "cleanup": cleanup_result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CommonsBook Disposable PostgreSQL Race Lab Demonstration"
    )
    parser.add_argument(
        "--admin-url",
        required=True,
        help="Admin connection URL to PostgreSQL (no fallback to application/production DB)",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Unique execution identifier (letters, digits, dashes, underscores)",
    )
    parser.add_argument(
        "--approval-file",
        required=True,
        help="Path to signed approval JSON file authorizing execution and cleanup",
    )
    parser.add_argument(
        "--approval-token",
        help="Explicit approval token (validated against approval file)",
    )
    parser.add_argument(
        "--evidence-dir",
        help="Directory to archive SQL and machine-readable evidence",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip database cleanup (leaves disposable database for manual inspection)",
    )

    args = parser.parse_args()

    # Redacted command string for evidence recording - never records raw token or temporary paths
    sanitized_admin = sanitize_dsn(args.admin_url)
    approval_file_display = (
        Path(args.approval_file).name if args.approval_file else "approval.json"
    )
    redacted_cmd = (
        f"python scripts/race_demo.py --admin-url {sanitized_admin} "
        f"--run-id {args.run_id} --approval-file {approval_file_display}"
    )

    effective_token = (
        args.approval_token
        or os.environ.get("RACE_LAB_APPROVAL_TOKEN")
        or os.environ.get("APPROVAL_TOKEN")
    )

    try:
        result = asyncio.run(
            async_main_logic(
                admin_url=args.admin_url,
                run_id=args.run_id,
                approval_file=args.approval_file,
                approval_token=effective_token,
                evidence_dir=args.evidence_dir,
                cleanup=not args.no_cleanup,
                command_str=redacted_cmd,
            )
        )
        print(f"Race lab demonstration completed successfully: {result['run_id']}")
        b_count = result["before"]["active_committed_rows_count"]
        print(f"Before (unprotected): {b_count} active overlaps proven")
        print(
            f"After (protected): 1 commit, 1 23P01 ({result['after']['lab_constraint_name']})"
        )
        if result.get("cleanup"):
            print(
                f"Cleanup: ephemeral database {result['database']} dropped and verified"
            )
    except RaceLabSafetyError as e:
        print(f"Race Lab Safety Error: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Race Lab Execution Failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
