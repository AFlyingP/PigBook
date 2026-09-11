# Observability

This document details the monitoring, telemetry, logging, and health readiness architecture of CommonsBook.

## 1. Readiness and Health Checks

The application exposes two un-prefixed health and readiness endpoints:

- `GET /healthz`: Lightweight liveness probe returning `{"status": "ok", "version": "<RELEASE_SHA>"}`.
- `GET /readyz`: Readiness probe verifying live database connectivity and schema compatibility against `backend/app/db/schema_compatibility.json`.
  - **Success (200 OK)**: Returns `{"status": "ready", "version": "<RELEASE_SHA>"}`.
  - **Failure (503 Service Unavailable)**: When the database is unreachable, queries time out (>2 seconds), or an unsupported/multiple/missing Alembic revision head is detected, returns status 503 with the standard error envelope:
    ```json
    {
      "error": {
        "code": "RETRYABLE_UNAVAILABLE",
        "message": "Service unavailable",
        "details": {}
      }
    }
    ```
    and sends header `Retry-After: 1`. No database error details or table internals are exposed in the response.

## 2. Metrics Architecture

Prometheus metrics are exposed at `GET /metrics` under the `Policy.metrics` access policy:
- Requires HTTP header `Authorization: Bearer <METRICS_TOKEN>`.
- The metrics token must be at least 32 bytes and is required in production.
- Requests with absent, malformed, or incorrect tokens, as well as user JWT tokens, are rejected with HTTP 401.
- Valid requests return Prometheus text exposition format (`text/plain; version=0.0.4`).

### 2.1 Metric Definitions

| Metric Name | Type | Labels | Description |
|---|---|---|---|
| `commonsbook_http_requests_total` | Counter | `method`, `route`, `status_class` | Total HTTP requests handled. `route` is strictly the route template (e.g. `/api/v1/resources/{id}`), never raw paths containing UUIDs. |
| `commonsbook_http_request_duration_seconds` | Histogram | `method`, `route` | Latency histogram with exact buckets: `.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30`. |
| `commonsbook_booking_conflicts_total` | Counter | `operation` | Real PostgreSQL 23P01 exclusion conflicts translated across `create`, `blackout`, and `promote`. |
| `commonsbook_idempotency_total` | Counter | `outcome` | Idempotency execution outcomes: `new` (new key acquired), `replay` (cached replay returned), or `mismatch` (payload hash mismatch). |
| `commonsbook_outbox_pending` | Gauge | none | Current number of pending outbox events. |
| `commonsbook_outbox_lag_seconds` | Gauge | none | Age in seconds of the oldest pending or processing outbox item (0 if none). |
| `commonsbook_outbox_dead` | Gauge | none | Count of dead outbox records requiring administrative intervention. |
| `commonsbook_outbox_deliveries_total` | Counter | `outcome` | Outbox delivery receipts (`sent` or `skipped`). |
| `commonsbook_expired_holds_pending` | Gauge | none | Offered bookings with overdue hold expirations awaiting worker processing. |
| `commonsbook_worker_heartbeat_age_seconds` | Gauge | none | Seconds elapsed since the primary worker heartbeat was recorded. |
| `commonsbook_db_pool_checked_out` | Gauge | none | Number of active checked-out connections in the SQLAlchemy connection pool. |
| `commonsbook_metrics_collection_age_seconds` | Gauge | none | Staleness indicator: seconds elapsed since successful database metric collection. |

Operational metrics are refreshed with a 15-second cache and a 1-second query timeout. If database collection times out or fails, the failure is logged and the existing gauge values remain with an incrementing `commonsbook_metrics_collection_age_seconds` staleness indicator.

## 3. Structured JSON Logging

All application log records are emitted to standard output as single-line JSON with strictly 14 fields:
```json
{
  "timestamp": "2026-09-11T12:00:00.000000Z",
  "level": "INFO",
  "service": "api",
  "release_sha": "0123456789abcdef0123456789abcdef01234567",
  "event": "http_request",
  "request_id": "7b843180-2a3b-4819-8669-7c8152e0be30",
  "route": "/api/v1/bookings",
  "method": "POST",
  "status_code": 201,
  "duration_ms": 14.25,
  "trace_id": null,
  "error_code": null,
  "outbox_id": null,
  "attempt": null
}
```

### 3.1 Redaction & Privacy Rules
- Log formatters automatically redact values for `Authorization`, `Cookie`, `password`, `invitation_token`, and SMTP passwords.
- Request and response bodies, cookies, and raw email addresses are never logged.
- Incoming `X-Request-ID` headers are accepted only when formatted as valid RFC 4122 UUIDs; malformed or missing headers cause a fresh UUID v4 to be generated.

## 4. Error Tracking (Sentry)

- Backend and frontend error tracking is initialized only when `SENTRY_DSN` (or `VITE_SENTRY_DSN`) is set.
- PII transmission is disabled (`send_default_pii=false`).
- `traces_sample_rate` is set to 0.1, and uncaught errors are reported at 1.0.
- All request headers, query strings, and payloads are filtered prior to transmission.
- When `VITE_SENTRY_DSN` is unset or empty, the frontend makes zero telemetry network requests.

## 5. Local Observability Stack

A local Grafana and Prometheus environment can be launched via Docker Compose:

```bash
docker compose -f compose.yaml -f infra/compose.observability.yaml --profile observability up -d
```

- **Prometheus**: Accessible at `http://127.0.0.1:9090` scraping the API `/metrics` endpoint every 15 seconds.
- **Grafana**: Accessible at `http://127.0.0.1:3000` with pre-provisioned datasource, dashboard, and alerts:
  - **Datasource**: Configured via `infra/grafana/datasources.yaml` pointing to the local Prometheus instance at `http://prometheus:9090` (uid: `prometheus`).
  - **Dashboard**: Configured via `infra/grafana/dashboards.yaml` loading the versioned dashboard from `infra/grafana/commonsbook.json` (uid: `commonsbook-main`). Verify via HTTP: `curl http://127.0.0.1:3000/api/search` lists the `CommonsBook Observability` dashboard.
  - **Alert Rules**: Configured via `infra/grafana/alerts.yaml` under the `commonsbook-alerts` group in the `CommonsBook` folder. Verify via HTTP: `curl http://127.0.0.1:3000/api/v1/provisioning/alert-rules` lists all seven provisioned alert rules.
  - **Deployment Annotations**: Configured using Grafana-native tag-based annotations (`tags: ["deployment"]`). Deployments push an event with the `deployment` tag and `release_sha` text to `/api/annotations`, which Grafana plots natively across time-series panels without requiring metric labels.

### 5.1 Alert Rules

The provisioned alert configuration (`infra/grafana/alerts.yaml`) defines all seven required alert conditions with exact thresholds and evaluation durations:
1. **HighErrorRate5xx**: 5xx HTTP response rate > 2% for 5m with at least 100 requests (`for: 5m`, severity: critical).
2. **HighLatencyP95**: p95 request latency > 1s for 10m with at least 100 requests (`for: 10m`, severity: warning).
3. **OutboxLagHigh**: Oldest pending/processing outbox event lag > 120s for 5m (`for: 5m`, severity: warning).
4. **OutboxDeadEvents**: Dead outbox event count > 0 for 5m (`for: 5m`, severity: critical).
5. **WorkerHeartbeatStale**: Background worker heartbeat age > 90s for 2m (`for: 2m`, severity: critical).
6. **ExpiredHoldsPendingOverdue**: Overdue hold bookings pending cleanup > 0 for 2m (`for: 2m`, severity: warning).
7. **MetricsCollectionStale**: Operational metric collector staleness > 60s for 2m (`for: 2m`, severity: warning).

## 6. Alloy Telemetry Sidecar Configuration

Grafana Alloy runs inside the worker container and scrapes the API `/metrics` endpoint before forwarding via Prometheus remote-write:
- **Target Configuration**: `API_INTERNAL_URL` defines the internal endpoint URL (e.g. `http://commonsbook-api:10000` on Render or `http://127.0.0.1:10000` locally).
- **Target Normalization**: Alloy uses `discovery.relabel` with regular expressions to strip any URL scheme prefix (`http://` or `https://`) and paths, producing a valid `host:port` scrape target. If `API_INTERNAL_URL` is empty or unset, it safely falls back to `127.0.0.1:10000`.
- **Scrape Interval**: Scrapes every 15 seconds using `Authorization: Bearer <METRICS_TOKEN>`.
- **Remote-Write**: Forwards to `GRAFANA_REMOTE_WRITE_URL` authenticated via `GRAFANA_REMOTE_WRITE_USER` and `GRAFANA_REMOTE_WRITE_TOKEN`.
