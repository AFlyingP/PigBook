# Production Deployment

This document specifies the deployment architecture, configuration contract, container packaging, and release procedures for CommonsBook.

## 1. Cloud Architecture and Topology

CommonsBook is deployed to Render in the **Oregon (us-west)** region with the following services:

1. **Web Service**: A single-origin Docker service serving FastAPI and the compiled React frontend from a single port.
2. **Worker Service**: A separate background worker running the outbox dispatcher, hold expiry scheduler, heartbeat monitor, and an embedded Grafana Alloy telemetry sidecar.
3. **Managed Database**: Managed PostgreSQL 16 with `btree_gist` extension and durable transaction storage.

## 2. Single Production Image Architecture

The application packages the web service, compiled frontend, worker daemon, and Grafana Alloy sidecar into a single `linux/amd64` multi-stage container image defined in `infra/Dockerfile.api`:

- **Stage 1 (Frontend Builder)**: Compiles the React/TypeScript frontend using locked Node 22 and npm dependencies into static assets in `/app/frontend/dist`.
- **Stage 2 (Runtime Container)**:
  - Base: Debian-slim Python 3.12.
  - Package Manager: Locked `uv` installer for synchronized Python wheels.
  - Pinned Binaries: Grafana Alloy `v1.3.1` for metrics scraping and remote-write.
  - Security: Runs as non-root user (`appuser`, UID 10001).
  - Web Server: Single Uvicorn process binding `PORT` (default `10000`).

### 2.1 Single-Origin Routing Contract

To simplify session cookies and eliminate CORS complexity in production, the API service also serves the compiled frontend under the same origin:

- Registered API routes (`/api/v1/*`), health checks (`/healthz`), readiness checks (`/readyz`), and metrics (`/metrics`) take strict precedence over static routes.
- Unknown API endpoints under `/api/*` return the standard JSON 404 error envelope:
  ```json
  {"error": {"code": "NOT_FOUND", "message": "Not found", "details": {}}}
  ```
  Unknown API routes never return `index.html`.
- Client-side deep links (e.g. `/resources/123`, `/login`) fall back to `/app/frontend/dist/index.html` with status 200.
- Static assets under `/assets/*` are served with appropriate MIME types.

### 2.2 Worker Process Supervision

The worker container uses `scripts/worker_entrypoint.sh` to supervise both the Python worker daemon and the Alloy sidecar:

- Propagates `SIGTERM` and `SIGINT` signals to both child processes for graceful shutdown.
- Fails fast and terminates the container with non-zero exit code if either child process dies unexpectedly.

## 3. Configuration Contract

Production configurations are validated on startup and fail closed:

| Setting | Validation Rule | Description |
|---|---|---|
| `APP_ENV` | Enforced enum: `local`, `test`, `production` | Enforces production startup validation when set to `production`. |
| `JWT_SECRET` | Enforced >= 32 bytes in production | Secret for signing HS256 access tokens. |
| `RATE_LIMIT_HMAC_SECRET` | Enforced >= 32 bytes in production | Secret for hashing IP and email in rate-limiting buckets. |
| `METRICS_TOKEN` | Enforced >= 32 bytes in production | Operational bearer token required to scrape `/metrics`. |
| `SENTRY_DSN` | Enforced nonempty in production | Backend Sentry DSN for API and worker error reporting. |
| `TEST_PROFILE` | Enforced not `race` in production | Rejects race test harness configuration in production. |
| `GRAFANA_REMOTE_WRITE_URL` | Enforced HTTPS URL when configured | Target URL for Alloy Prometheus remote-write. |
| `APP_ORIGIN` | URL (default `http://localhost:5173`) | CORS origin enforcement for cookies; set to deployment HTTPS domain in production. |
| `DATABASE_URL` | SQLAlchemy URL | Application PostgreSQL connection URL; required for runtime database operations. |
| `PORT` | Integer (default 10000) | Port bound by Uvicorn process in production. |

Startup validation strictly enforces that `JWT_SECRET`, `RATE_LIMIT_HMAC_SECRET`, `METRICS_TOKEN`, and `SENTRY_DSN` are present and valid in production; missing or invalid values immediately abort process initialization.

## 4. Schema Compatibility and Zero-Downtime Releases

Each production container contains `backend/app/db/schema_compatibility.json` specifying supported database schema revisions:

```json
{
  "supported_revisions": [
    "0004_operations"
  ]
}
```

- **Readiness Probe (`GET /readyz`)**: Queries `alembic_version` on the live database before routing traffic.
- **Strict Allowlist**: Validates that exactly one revision head exists and is present in the allowlist.
- **Fail Closed**: If the revision is unknown, multiple heads exist, or `alembic_version` is empty, `/readyz` returns 503 `RETRYABLE_UNAVAILABLE` with `Retry-After: 1`.
- **No Lexical Comparisons**: Revision matching uses exact allowlist membership, never min/max lexical comparisons.

## 5. Local Build and Verification

Build and run the production image locally using Docker:

```bash
# Build the production image
docker build -t commonsbook:latest -f infra/Dockerfile.api .

# Run the container with local configuration
docker run -d --rm \
  -p 10000:10000 \
  -e APP_ENV=local \
  -e PORT=10000 \
  commonsbook:latest

# Verify health endpoint
curl http://localhost:10000/healthz
```
