# CommonsBook

CommonsBook is an equipment and room reservation service designed for a single community group. One bookable resource represents an indivisible item or space with unit capacity.

## Current State

The service currently implements core authentication, resource management, and booking creation primitives:
- **Authentication & Authorization**: Registration via single-use invitation tokens, password login (Argon2id), rotating refresh token families in HttpOnly cookies, logout with family revocation, centralized role-based access control, and atomic PostgreSQL-backed rate limiting.
- **Resource Management**: Active resource catalog pagination, detail lookups with version-based `ETag`, and occupied availability interval queries on 30-minute UTC boundaries.
- **Atomic Idempotent Booking Creation**: `POST /api/v1/bookings` creates confirmed reservations backed by a GiST exclusion constraint (`bookings_no_overlap`), preventing overlapping reservations without application preflight checks. Requests require a UUID v4 `Idempotency-Key`; the idempotency record, booking, and transactional outbox event commit atomically in a single transaction. Replays return stored responses without re-executing business logic.
- **Automated Verification**: Complete verification suite including unit tests, isolated PostgreSQL 16 integration tests, centralized permission matrix verification, concurrency race gates, and a disposable PostgreSQL race lab gate.
- **Disposable PostgreSQL Race Lab**: Standalone demonstration script (`scripts/race_demo.py`, detailed in [`docs/race-lab.md`](docs/race-lab.md)) reproducing check-then-insert double-booking vulnerabilities under real concurrency on unprotected tables and proving mutual exclusion invariant enforcement via PostgreSQL GiST exclusion constraints (`bookings_no_overlap`, SQLSTATE `23P01`) within dedicated disposable database namespaces.

## Prerequisites

- Python 3.12
- Node.js 22 LTS
- uv (Python package and virtual environment manager)
- Docker and Docker Compose
- PostgreSQL 16 (executed via Docker)

## Architecture

CommonsBook is structured as a modular monolith:

- **Backend (`backend/`)**: Python 3.12 FastAPI service structured by domain package boundaries (`auth`, `resources`, `bookings`, `waitlist`, `notifications`, `admin`, `db`, `observability`).
- **Frontend (`frontend/`)**: React 18, TypeScript, Vite, Material UI v5 with Emotion.
- **Single Origin**: In production, the backend serves both API endpoints and compiled frontend assets under the same origin, simplifying authentication cookie handling and cross-origin controls.

## Quickstart

### Installation

Install locked dependencies:

```bash
# Backend dependencies (Python 3.12)
cd backend
uv sync --frozen
cd ..

# Frontend dependencies (Node 22)
cd frontend
npm ci
cd ..
```

Alternatively, run the verification tool bootstrap:

```bash
python scripts/verify.py bootstrap
```

## Verification

The repository verification runner validates formatting, linting, type safety, integration behavior, permissions, and concurrency invariants:

```bash
# Verify code formatting, linting, and type checking
python scripts/verify.py lint

# Run unit test suites
python scripts/verify.py unit

# Run isolated PostgreSQL integration test suites
python scripts/verify.py integration

# Run 200-user concurrency race and same-key idempotency gate
python scripts/verify.py concurrency --fresh

# Run centralized permission matrix verification
python scripts/verify.py permissions --fresh

# Run disposable PostgreSQL race lab demonstration gate
python scripts/verify.py race-lab --fresh

# Run full regression suite across all implemented gates
python scripts/verify.py regression --fresh
```

### Verification Results

Fresh verification of source `96365746d7cee5b2cd4f06795e005f729fffa8c0` ran on local Windows 11 with Docker PostgreSQL 16.15:

- **Lint & Typecheck**: Passed across all Python and TypeScript sources (Ruff, Mypy strict mode, ESLint, TypeScript compiler).
- **Unit Tests**: 91 backend unit tests and 3 frontend component tests passed.
- **Integration Tests**: 92 database integration tests passed in isolated PostgreSQL 16 containers.
- **Concurrency Gate**: Three independent 200-user same-slot rounds each produced exactly one `201 Created` and 199 `409 SLOT_CONFLICT` responses. A separate 200-request identical-key run returned 200 stored-equivalent `201` responses with one initial execution and 199 replays, persisting one booking, one idempotency key, and one outbox event.
- **Permissions Gate**: 2 permission tests passed; all 11 registered endpoints were verified against the centralized permissions matrix.
- **Performance / Load / Real-User Feedback**: Not yet measured. No production latency or throughput claim is made from the local correctness gate.

### Standalone Disposable Race Lab Verification

Fresh verification of implementation source `8d46afaed5c3c211350ead66f29986264c7a5825` ran on local Windows 11 with Docker PostgreSQL 16.15 (subsequent documentation-only bytes were not lab rerun inputs):

- **Command**: `python scripts/verify.py race-lab --fresh`
- **Before Phase (Unprotected check-then-insert)**: Concurrency vulnerability reproduced on `race_lab.bookings_unprotected` (2 overlapping rows committed under application preflight checks; 1 overlapping pair detected).
- **After Phase (Protected with GiST exclusion constraint)**: Mutual exclusion deterministically enforced by PostgreSQL GiST exclusion constraint `bookings_no_overlap` on `race_lab.bookings_protected` (Writer 1 committed, Writer 2 rejected with SQLSTATE `23P01`; exactly 1 active booking committed).
- **Cleanup**: Ephemeral database namespace (`commonsbook_racelab_gate_8df458e5d718`) dropped and verified nonexistent via `pg_database` query; zero residual containers or networks.
- **Ticket Gate**: `python scripts/verify.py ticket --ticket T-012 --fresh` passed (86 passed: 56 safety/authorization/isolation tests, 30 runner verification tests).

## Known Limitations

- Background worker daemon executes hold expiry, heartbeat monitoring, hourly database maintenance, and the outbox notification dispatcher; email delivery is wired but disabled by default until provider configuration is enabled.
- Administrative management endpoints (resource creation, user modification, blackout scheduling) are not yet exposed.
