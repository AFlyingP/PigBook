# Disposable PostgreSQL Race Lab

This document describes the design, strict safety contracts, execution workflow, and authentic verification results of the Disposable PostgreSQL Race Lab (`scripts/race_demo.py`).

---

## 1. Overview and Purpose

The Disposable PostgreSQL Race Lab demonstrates the concrete failure mode of application-level concurrency checks and verifies the invariant enforcement provided by PostgreSQL GiST exclusion constraints under real, synchronized database concurrency.

### Core Objectives

1. **Vulnerability Demonstration (Before)**:
   Proves that application-level check-then-insert logic (`SELECT count(*) ...` followed by `INSERT`) fails under concurrent execution at standard `READ COMMITTED` isolation, resulting in double-booking (two active overlapping reservations committed for the same resource).
2. **Invariant Verification (After)**:
   Proves that PostgreSQL's native GiST exclusion constraint (`bookings_no_overlap`) deterministically enforces the mutual exclusion invariant, permitting exactly one commit and rejecting concurrent overlapping inserts with SQLSTATE `23P01` (`ExclusionViolationError`).
3. **Safety and Isolation**:
   Executes in a dedicated, ephemeral, isolated database namespace (`commonsbook_racelab_<run_id>`) with strict fail-closed safety checks, requiring granular authorization tokens, loopback-only connections, and guaranteed verified post-run cleanup.

---

## 2. Strict Safety Architecture and Invariants

To eliminate any possibility of accidental data loss or disruption to application, staging, or production environments, the race lab enforces the following safety invariants:

### 1. No Production DSN Fallback
- The CLI and verification runner strictly require an explicit `--admin-url` parameter.
- No fallback to environment variables like `DATABASE_URL` or configuration files is permitted.

### 2. Strict Loopback Host Validation
- Database connection strings must resolve to loopback interfaces (`127.0.0.1`, `localhost`, `::1`, or `127.0.0.0/8`).
- External IP addresses, public hostnames, cloud database endpoints, `host.docker.internal`, and arbitrary container names are rejected before establishing any database connection or issuing SQL.

### 3. Dedicated Disposable Namespace
- Target database names must strictly follow the prefix `commonsbook_racelab_<run_id>`, where `<run_id>` is an alphanumeric identifier.
- Protected database names (`commonsbook`, `commonsbook_production`, `commonsbook_staging`, `commonsbook_test`, `postgres`, `template0`, `template1`) and any name containing `production`, `prod`, or `staging` are strictly forbidden and rejected before connection.

### 4. Granular Separately Authorized Actions
- Execution requires a signed JSON approval file containing a cryptographic token (minimum 16 characters), run ID, target database name, host, and an explicit list of authorized actions (`create`, `reset`, `cleanup`).
- **Approval Secret Privacy**: Approval material remains strictly private and redacted. Production verification tooling and execution manifests record only the token SHA-256 hash, never raw tokens or database credentials. In persisted evidence, `approval.json` records `token_sha256`, while `race_demo` receives actual material via a temporary file outside evidence.
- **Separation of Create, Reset, and Drop Semantics**:
  - Fresh creation (`create`) refuses to touch or drop an existing database. If the target database already exists, creation halts immediately with a safety error.
  - A destructive drop of an existing database during creation requires an explicit, separately authorized `reset` action in the approval file.
  - Ephemeral cleanup requires an explicit `cleanup` action.

### 5. Verified Post-Run Cleanup
- When cleanup is authorized, the ephemeral database is dropped and connections are terminated.
- The runner queries `pg_database` on the maintenance connection to prove that the database no longer exists before completing.

---

## 3. Demonstration Mechanics

The demonstration provisions a dedicated `race_lab` schema containing two isolated tables:

### Unprotected Table (`race_lab.bookings_unprotected`)
Contains standard range validity checks and status checks, but **no exclusion constraint**:

```sql
CREATE TABLE race_lab.bookings_unprotected (
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
);
```

Two concurrent transactions execute synchronized check-then-insert sequences:
1. `SELECT count(*) FROM race_lab.bookings_unprotected WHERE resource_id = :id AND time_range && :range AND status IN ('confirmed', 'offered')`
2. Both transactions synchronize across an `asyncio.Barrier(2)`, confirming both observed 0 overlaps.
3. Both execute `INSERT INTO race_lab.bookings_unprotected ...` and `COMMIT`.
4. **Result**: Both commit successfully, persisting two active overlapping bookings.

### Protected Table (`race_lab.bookings_protected`)
Includes the exact production GiST exclusion invariant:

```sql
CREATE TABLE race_lab.bookings_protected (
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
    CONSTRAINT bookings_no_overlap EXCLUDE USING gist (
        resource_id WITH =,
        time_range WITH &&
    ) WHERE (status IN ('confirmed','offered'))
);
```

Both concurrent transactions execute identical check-then-insert sequences against `race_lab.bookings_protected`:
1. Both evaluate 0 overlaps before insert.
2. Writer 1 commits successfully.
3. Writer 2 raises an `ExclusionViolationError` with PostgreSQL SQLSTATE `23P01` naming constraint `bookings_no_overlap`.
4. **Result**: Exactly one booking commits; double-booking is deterministically prevented at the database engine level.

---

## 4. Execution and Verification

The race lab is integrated into the repository verification framework via `scripts/verify.py`:

```bash
# Execute the race-lab verification gate in an ephemeral Docker PostgreSQL container
python scripts/verify.py race-lab --fresh

# Execute the dedicated ticket T-012 safety test suite
python scripts/verify.py ticket --ticket T-012 --fresh
```

---

## 5. Verification Results

Reproducible verification command:

```bash
python scripts/verify.py race-lab --fresh
```

Compact verification result for implementation source `8d46afaed5c3c211350ead66f29986264c7a5825` (local Windows 11, Docker PostgreSQL 16.15; subsequent documentation-only bytes were not lab rerun inputs):

- **Target Database**: Ephemeral disposable database (`commonsbook_racelab_gate_8df458e5d718`, loopback host `127.0.0.1`).
- **Before Phase (Unprotected check-then-insert)**:
  - Table: `race_lab.bookings_unprotected`
  - Outcome: Concurrency vulnerability reproduced. Both concurrent transactions observed 0 overlaps and committed successfully, persisting 2 overlapping active reservations (1 overlapping pair detected).
- **After Phase (Protected with GiST exclusion constraint)**:
  - Table: `race_lab.bookings_protected`
  - Constraint: `bookings_no_overlap`
  - Outcome: Mutual exclusion invariant enforced. Writer 1 committed, Writer 2 was rejected by PostgreSQL with SQLSTATE `23P01` (`ExclusionViolationError`) naming constraint `bookings_no_overlap`. Exactly 1 active booking committed.
- **Cleanup**:
  - Ephemeral database dropped and confirmed nonexistent via `pg_database` query.
  - Zero residual Docker containers or compose project resources.
- **Evidence Integrity**:
  - Evidence directory: `evidence/20260908T024205Z_34bd94c44d39`
  - Input fingerprint: `bc8ae85a5f85aaf492b3fb367c29637e9d9e5536ea64315309400b02490d08bd`
  - Secrets and raw approval tokens are redacted; approval record stores `token_sha256` (`185ac7a26921b6b63f1bb7387688a5e4b96b3b1fabd7a87b27e3be8f0af91470`).

Safety gate verification:

```bash
python scripts/verify.py ticket --ticket T-012 --fresh
```

- **Evidence Integrity**: Evidence directory `evidence/20260908T024214Z_bc987ad1474d` (input fingerprint `06eb3a17c201730219ea175c9a27a0127bcceb75f12daf78356e7329af2a1baf`).
- **Test Results**: 86 passed (56 safety/authorization/isolation integration tests, 30 runner verification unit tests). All safety refusal contracts (unauthorized hosts, non-disposable database names, missing/malformed approval tokens, missing cleanup actions), isolation guarantees, and invariant checks pass.
