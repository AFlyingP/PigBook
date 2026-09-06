# Testing and Verification

This document details the test infrastructure, isolation architecture, verification targets, and evidence caching mechanisms implemented in CommonsBook.

## Verification Targets

Repository verification is orchestrated by `scripts/verify.py`. Supported targets include:

- **`bootstrap`**: Synchronizes backend dependencies using frozen pins in `backend/uv.lock` via `uv sync` and frontend dependencies from `frontend/package-lock.json` via `npm ci`.
- **`lint`**: Runs code style and static type validation:
  - Ruff linter check: `uv run --project backend ruff check backend`
  - Ruff formatting check: `uv run --project backend ruff format --check backend`
  - Mypy static type checking: `uv run --project backend mypy backend/app`
  - ESLint JavaScript/TypeScript check: `npm --prefix frontend run lint`
  - TypeScript compiler check: `npm --prefix frontend run typecheck`
- **`unit`**: Executes isolated fast test suites:
  - Backend unit tests via pytest: `uv run --project backend pytest backend/tests/unit`
  - Frontend component tests via Vitest: `npm --prefix frontend test -- --run`
- **`integration`**: Starts an isolated PostgreSQL 16 test container and runs database-backed integration suites via pytest (`backend/tests/integration`), ensuring complete namespace teardown upon completion.
- **`regression`**: Executes all implemented gates in sequence (`lint` -> `unit` -> `integration`), skipping gates designated for future changes.

### Fresh Execution vs Cached Execution

By default, verification targets check for reusable matching evidence. To force fresh execution of all commands regardless of cache validity, provide the `--fresh` flag:

```bash
python scripts/verify.py unit --fresh
```

## Test Isolation and Namespacing

### PostgreSQL Test Container Topology

Integration tests require real PostgreSQL 16. Test database environments are managed through `compose.test.yaml`:

- **Dedicated Namespace**: Each execution derives a unique project name (`cb_test_<hash>`) and database name from `TEST_RUN_ID` (or an automated timestamp-PID run identifier).
- **Port Allocation**: Host ports are not published by default. When host access is required for runner test suites, an ephemeral port is dynamically allocated and bound exclusively to loopback (`127.0.0.1`).
- **Memory-Backed Storage**: Database data resides entirely in container `tmpfs` memory mounts (`/var/lib/postgresql/data`). No host volumes or named Docker volumes are created.
- **Scoped Teardown**: Teardown commands target only the specific Compose project identifier created for that run (`docker compose -p <project> down -v --remove-orphans`), ensuring parallel test executions cannot collide and never impact local development containers.

### Transactional Rollback Fixtures

Integration test fixtures are defined in `backend/tests/conftest.py`:

- A session-scoped async engine is configured with `NullPool` so that connections are bound to the active asyncio event loop of each test case.
- During fixture initialization, the database engine verifies that the server dialect is `postgresql` and the major version is exactly `16`. Any non-PostgreSQL backend (such as SQLite) or version mismatch causes immediate test failure.
- A function-scoped transactional session wraps each test execution. Tests run inside an isolated transaction with savepoint support (`join_transaction_mode="create_savepoint"`).
- Upon test completion, the outer transaction is rolled back unconditionally. No state changes persist across test boundaries.

## Verification Fingerprinting and Evidence Reuse

To eliminate redundant test execution without compromising integrity, the verification runner implements content-based input fingerprinting and evidence reuse:

- **Input Fingerprint Calculation**: The fingerprint for any gate is computed as a deterministic SHA-256 digest over the complete relevant input set. Files are processed in strictly sorted lexicographical order. Each file entry incorporates its relative POSIX path, tracked permission mode, and exact binary contents.
- **No Timestamp Dependencies**: File modification times (mtime) and directory traversal orders are excluded from fingerprint computation. Changing a file timestamp without modifying its contents preserves the fingerprint.
- **Strict Reuse Validation**: Evidence reuse is allowed only when:
  1. The computed input fingerprint matches the source run fingerprint exactly.
  2. Runtimes and tool versions (Python launcher, uv, Node.js, npm) are identical.
  3. The active test profile matches.
  4. Dependency lockfiles match.
  5. The source run completed with an exit code of 0.
- **Truthful Provenance**: When an existing run is reused, the emitted manifest explicitly records `reused: true`, the original source commit SHA (`source_verified_sha`), the candidate commit SHA, and the cryptographic hash of the source manifest. Reused runs are never represented as fresh executions.

## No-Skips Enforcement Policy

Verification suites enforce a strict zero-skips policy:

- **Pytest**: If any test is marked as skipped, or if zero tests are collected in a suite, the verification runner marks the run as a failure.
- **Vitest**: If any test is skipped or if no tests are found, the execution fails.

## Current Status and Limitations

The following verification gates are currently implemented:
- `bootstrap`
- `lint`
- `unit`
- `integration`
- `regression`

The `concurrency` and `permissions` gates are defined in the pipeline sequence but remain unimplemented at this stage. Attempting to execute them directly will exit with an explicit message indicating they are not yet implemented.
