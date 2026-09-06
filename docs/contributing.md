# Contributor Guide

This guide describes development prerequisites, local verification commands, continuous integration stages, branch protection setup, and development conventions for CommonsBook.

## Prerequisites

Before contributing, ensure your local environment provides the following tools:

- **Python 3.12**: Runtime for backend services and verification tooling.
- **uv 0.12.10**: Fast Python package manager used for backend virtual environment and dependency resolution.
- **Node.js 22 and npm**: Runtime and package manager for frontend development and testing.
- **Docker and Docker Compose**: Required for running ephemeral PostgreSQL 16 test instances during integration verification and building container images.
- **Git**: Version control.

## Local Verification

CommonsBook uses `scripts/verify.py` as the canonical verification runner across development environments and automated workflows.

### Bootstrap

Before executing test suites or linters, install and synchronize backend and frontend dependencies:

```bash
python scripts/verify.py bootstrap
```

This command synchronizes backend dependencies via `uv sync --frozen --python 3.12 --project backend` and frontend dependencies via `npm ci --prefix frontend`.

### Running Verification Targets

Run individual verification targets as follows:

- **Linting and Type Checking**:
  ```bash
  python scripts/verify.py lint
  ```
  Executes:
  - Ruff linter check: `uv run --project backend ruff check backend`
  - Ruff format check: `uv run --project backend ruff format --check backend`
  - Mypy static type checking: `uv run --project backend mypy backend/app`
  - ESLint checks: `npm --prefix frontend run lint`
  - TypeScript compiler checks: `npm --prefix frontend run typecheck`

- **Unit Tests**:
  ```bash
  python scripts/verify.py unit
  ```
  Executes backend unit tests via `pytest` (`backend/tests/unit`) and frontend component tests via `vitest` (`frontend/tests`).

- **Integration Tests**:
  ```bash
  python scripts/verify.py integration
  ```
  Spins up an isolated, namespaced PostgreSQL 16 container via Docker Compose on loopback (`127.0.0.1`), executes database integration tests via `pytest` (`backend/tests/integration`), and unconditionally cleans up container resources upon completion.

- **Container Image Build**:
  ```bash
  python scripts/verify.py build
  ```
  Builds the production API container image locally using `infra/Dockerfile.api` without pushing to any registry.

- **Full Regression Pipeline**:
  ```bash
  python scripts/verify.py regression
  ```
  Runs all implemented gates sequentially (`lint` -> `unit` -> `integration`). Gates that are registered but not yet implemented are skipped during regression.

### Evidence and Caching

Every execution of `scripts/verify.py` writes a structured execution manifest and command logs to `evidence/<run-id>/`. Input fingerprinting is content-based (path, file mode, byte contents; never timestamps).

Evidence reuse is currently disabled: every gate executes on every run. When it is later enabled, it will apply only to the concurrency and permissions gates, and a reused result is always recorded as reused with its original execution commit — never relabeled as a fresh run.

The `--fresh` flag exists and forces execution, disabling reuse:

```bash
python scripts/verify.py concurrency --fresh
```

## Continuous Integration (CI)

Continuous integration runs on GitHub Actions on every pull request and on every push to `main`.

### Concurrency and Permissions

- **Permissions**: The workflow operates under strict least-privilege defaults (`contents: read`). No write permissions and no external secrets are granted.
- **Concurrency**: Workflows are grouped by branch or pull request reference (`${{ github.workflow }}-${{ github.ref }}`). Redundant runs on pull requests cancel automatically, while runs on the `main` branch are never canceled in progress.

### Stage Order

CI stages execute in a strict linear sequence enforced by job dependencies (`needs`):

1. **`lint` (`Lint and Typecheck`)**:
   Runs linting, code formatting checks, and static type analysis across backend and frontend codebases.
2. **`unit` (`Unit Tests`)**:
   Runs fast, in-memory backend unit tests and frontend component tests. Depends on `lint`.
3. **`integration` (`Integration Tests`)**:
   Runs database-backed integration tests against a real PostgreSQL 16 service managed by Docker Compose. Depends on `unit`.
4. **`concurrency` (`Concurrency Gate`)**:
   Registered verification stage for multi-worker and concurrent state validation. Currently registered as not implemented; verifies registration honesty without blocking the pipeline. Depends on `integration`.
5. **`build` (`Build API Image`)**:
   Builds the API container image from `infra/Dockerfile.api` using Docker. No images are tagged for external registries or published. Depends on `concurrency`.

### Dependency Caching

CI caches strictly lockfile-keyed dependency data:
- Backend: cached on the SHA-256 hash of `backend/uv.lock`.
- Frontend: cached on the SHA-256 hash of `frontend/package-lock.json`.

Test outcomes, evidence manifests, and test execution artifacts are never cached across CI runs.

### Evidence Artifacts

Each stage uploads its generated `evidence/` directory as a workflow artifact named `evidence-<stage>` upon step completion or failure (`if: always()`), retained for 14 days.

## Required Status Checks and Branch Protection

To guarantee repository integrity and maintain a dependable mainline, branch protection must be configured on the `main` branch.

### Required Status Check Names

The exact check names that must pass before merging correspond to the job names defined in `.github/workflows/ci.yml`:

- `Lint and Typecheck`
- `Unit Tests`
- `Integration Tests`
- `Concurrency Gate`
- `Build API Image`

### Step-by-Step Branch Protection Configuration

Repository administrators should configure branch protection rules in GitHub using the following procedure:

1. Open the repository on GitHub and navigate to **Settings** -> **Branches**.
2. Under **Branch protection rules**, click **Add branch protection rule** (or edit the rule for `main`).
3. Set **Branch name pattern** to `main`.
4. Check **Require a pull request before merging**.
5. Check **Require status checks to pass before merging**.
6. Check **Require branches to be up to date before merging**.
7. In the search box labeled **Status checks that are required**, search and select each of the five checks:
   - `Lint and Typecheck`
   - `Unit Tests`
   - `Integration Tests`
   - `Concurrency Gate`
   - `Build API Image`
8. Check **Do not allow bypassing the above settings** to enforce rules uniformly.
9. Click **Create** (or **Save changes**).

## Development Conventions

### The Green-Main Convention

The `main` branch must remain green at all times. Every commit merged to `main` must pass all required verification checks. If a merge introduces an unforeseen failure or breaks `main`, the commit must be reverted immediately before subsequent work proceeds.

### Commit Message Conventions

Commit messages must adhere to the following format:

- **Subject Line**: A concise imperative summary under 72 characters describing what the commit does (e.g., `Add isolated database test fixture` or `Fix token validation edge case`). Use present tense and omit trailing punctuation.
- **Body** (optional): Separated from the subject by a blank line. Explains the motivation, context, and reasoning behind the change.

### Current Limitations and Status

- **Concurrency Gate**: The `concurrency` gate is registered in the verification sequence and monitored in CI. It is currently not implemented and activates when the concurrency suite lands. Regression runs enumerate only gates the manifest marks implemented, and explicitly report unimplemented gates as not implemented — never reporting them as passing.
- **Permissions Gate**: The `permissions` gate is defined in the pipeline architecture but remains unimplemented at this stage. Regression runs enumerate only gates the manifest marks implemented, and explicitly report unimplemented gates as not implemented — never reporting them as passing.
