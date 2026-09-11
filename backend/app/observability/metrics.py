import asyncio
import logging
import time
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth.dependencies import AuthorizedScope, Policy, authorize
from app.bookings.models import Booking
from app.db.session import get_sessionmaker
from app.notifications.models import NotificationDelivery, Outbox, WorkerHeartbeat

logger = logging.getLogger("app.observability.metrics")


# Prometheus Metric Singletons (reusing collectors if re-imported)
def _get_or_create_counter(
    name: str, documentation: str, labelnames: tuple[str, ...] = ()
) -> Counter:
    if name in REGISTRY._names_to_collectors:
        collector = REGISTRY._names_to_collectors[name]
        if isinstance(collector, Counter):
            return collector
    return Counter(name, documentation, labelnames=labelnames)


def _get_or_create_histogram(
    name: str, documentation: str, labelnames: tuple[str, ...] = (), buckets: tuple[float, ...] = ()
) -> Histogram:
    if name in REGISTRY._names_to_collectors:
        collector = REGISTRY._names_to_collectors[name]
        if isinstance(collector, Histogram):
            return collector
    return Histogram(name, documentation, labelnames=labelnames, buckets=buckets)


def _get_or_create_gauge(name: str, documentation: str, labelnames: tuple[str, ...] = ()) -> Gauge:
    if name in REGISTRY._names_to_collectors:
        collector = REGISTRY._names_to_collectors[name]
        if isinstance(collector, Gauge):
            return collector
    return Gauge(name, documentation, labelnames=labelnames)


DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

HTTP_REQUESTS_TOTAL = _get_or_create_counter(
    "commonsbook_http_requests_total",
    "Total HTTP requests handled by the service",
    labelnames=("method", "route", "status_class"),
)

HTTP_REQUEST_DURATION_SECONDS = _get_or_create_histogram(
    "commonsbook_http_request_duration_seconds",
    "HTTP request latency histogram in seconds",
    labelnames=("method", "route"),
    buckets=DURATION_BUCKETS,
)

BOOKING_CONFLICTS_TOTAL = _get_or_create_counter(
    "commonsbook_booking_conflicts_total",
    "Total booking reservation conflict attempts",
    labelnames=("operation",),
)

IDEMPOTENCY_TOTAL = _get_or_create_counter(
    "commonsbook_idempotency_total",
    "Total idempotency key evaluation outcomes",
    labelnames=("outcome",),
)

OUTBOX_PENDING = _get_or_create_gauge(
    "commonsbook_outbox_pending",
    "Current number of pending outbox events",
)

OUTBOX_LAG_SECONDS = _get_or_create_gauge(
    "commonsbook_outbox_lag_seconds",
    "Age of the oldest pending or processing outbox item in seconds (0 if none)",
)

OUTBOX_DEAD = _get_or_create_gauge(
    "commonsbook_outbox_dead",
    "Current number of dead outbox events",
)

OUTBOX_DELIVERIES_TOTAL = _get_or_create_counter(
    "commonsbook_outbox_deliveries_total",
    "Notification delivery outcomes recorded",
    labelnames=("outcome",),
)

EXPIRED_HOLDS_PENDING = _get_or_create_gauge(
    "commonsbook_expired_holds_pending",
    "Count of offered bookings whose holds have expired but are pending cleanup",
)

WORKER_HEARTBEAT_AGE_SECONDS = _get_or_create_gauge(
    "commonsbook_worker_heartbeat_age_seconds",
    "Seconds elapsed since last background worker heartbeat",
)

DB_POOL_CHECKED_OUT = _get_or_create_gauge(
    "commonsbook_db_pool_checked_out",
    "Number of database connections currently checked out of the pool",
)

METRICS_COLLECTION_AGE_SECONDS = _get_or_create_gauge(
    "commonsbook_metrics_collection_age_seconds",
    "Seconds since last successful operational metrics collection",
)


# Operational collection tracking
_last_collection_mono: float = -100.0
_last_collection_wall: float = 0.0
_last_delivery_counts: dict[str, int] = {"sent": 0, "skipped": 0}


def record_booking_conflict(operation: str) -> None:
    """Record a booking conflict (operation: create, blackout, promote)."""
    if operation in ("create", "blackout", "promote"):
        BOOKING_CONFLICTS_TOTAL.labels(operation=operation).inc()


def record_idempotency_outcome(outcome: str) -> None:
    """Record an idempotency outcome (outcome: new, replay, mismatch)."""
    if outcome in ("new", "replay", "mismatch"):
        IDEMPOTENCY_TOTAL.labels(outcome=outcome).inc()


def record_outbox_delivery(outcome: str) -> None:
    """Record an outbox delivery outcome (outcome: sent, skipped)."""
    if outcome in ("sent", "skipped"):
        OUTBOX_DELIVERIES_TOTAL.labels(outcome=outcome).inc()


async def refresh_operational_metrics(session: AsyncSession) -> None:
    """Refresh database-backed operational metrics from the active session.

    Fixed public interface (Spec 3.4).
    """
    global _last_collection_mono, _last_collection_wall, _last_delivery_counts

    # 1. Outbox pending count
    q_pending = select(func.count()).select_from(Outbox).where(Outbox.status == "pending")
    pending_count = (await session.execute(q_pending)).scalar_one() or 0
    OUTBOX_PENDING.set(pending_count)

    # 2. Outbox lag seconds (oldest pending or processing, 0 if none)
    q_lag = select(
        func.extract("epoch", func.clock_timestamp() - func.min(Outbox.occurred_at))
    ).where(Outbox.status.in_(("pending", "processing")))
    lag_val = (await session.execute(q_lag)).scalar_one_or_none()
    OUTBOX_LAG_SECONDS.set(float(lag_val) if lag_val is not None else 0.0)

    # 3. Outbox dead count
    q_dead = select(func.count()).select_from(Outbox).where(Outbox.status == "dead")
    dead_count = (await session.execute(q_dead)).scalar_one() or 0
    OUTBOX_DEAD.set(dead_count)

    # 4. Outbox delivery totals
    q_deliv = (
        select(NotificationDelivery.state, func.count())
        .where(NotificationDelivery.state.in_(("sent", "skipped")))
        .group_by(NotificationDelivery.state)
    )
    deliv_rows = (await session.execute(q_deliv)).fetchall()
    current_counts = {"sent": 0, "skipped": 0}
    for state, cnt in deliv_rows:
        if state in current_counts:
            current_counts[state] = int(cnt)

    for state, current_cnt in current_counts.items():
        prev_cnt = _last_delivery_counts.get(state, 0)
        diff = current_cnt - prev_cnt
        if diff > 0:
            OUTBOX_DELIVERIES_TOTAL.labels(outcome=state).inc(diff)
    _last_delivery_counts = current_counts

    # 5. Expired holds pending
    q_holds = (
        select(func.count())
        .select_from(Booking)
        .where(Booking.status == "offered", Booking.expires_at <= func.clock_timestamp())
    )
    holds_count = (await session.execute(q_holds)).scalar_one() or 0
    EXPIRED_HOLDS_PENDING.set(holds_count)

    # 6. Worker heartbeat age
    q_hb = select(func.extract("epoch", func.clock_timestamp() - WorkerHeartbeat.seen_at)).where(
        WorkerHeartbeat.name == "primary"
    )
    hb_val = (await session.execute(q_hb)).scalar_one_or_none()
    if hb_val is not None:
        WORKER_HEARTBEAT_AGE_SECONDS.set(float(hb_val))
    else:
        WORKER_HEARTBEAT_AGE_SECONDS.set(999999.0)

    # 7. DB connection pool checked out
    try:
        import app.db.session as db_sess

        if db_sess._engine is not None and hasattr(db_sess._engine.sync_engine, "pool"):
            pool = db_sess._engine.sync_engine.pool
            checked_out = getattr(pool, "checkedout", lambda: 0)()
            DB_POOL_CHECKED_OUT.set(checked_out)
        else:
            DB_POOL_CHECKED_OUT.set(0)
    except Exception:
        DB_POOL_CHECKED_OUT.set(0)

    now_mono = time.monotonic()
    now_wall = time.time()
    _last_collection_mono = now_mono
    _last_collection_wall = now_wall
    METRICS_COLLECTION_AGE_SECONDS.set(0.0)


class HttpMetricsMiddleware(BaseHTTPMiddleware):
    """Middleware recording HTTP request counts and durations against route templates."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        start_time = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start_time

        route_obj = request.scope.get("route")
        if route_obj and hasattr(route_obj, "path"):
            route_template = route_obj.path
        else:
            route_template = "unmatched"

        status_class = f"{response.status_code // 100}xx"
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method,
            route=route_template,
            status_class=status_class,
        ).inc()

        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=request.method,
            route=route_template,
        ).observe(duration)

        return response


def install_metrics(app: FastAPI) -> None:
    """Install metrics collection middleware and the /metrics endpoint.

    Fixed public interface (Spec 3.4).
    """
    app.add_middleware(HttpMetricsMiddleware)

    @app.get("/metrics", response_model=None)
    async def metrics(
        _scope: AuthorizedScope = Depends(authorize(Policy.metrics)),
    ) -> Response:
        global _last_collection_mono, _last_collection_wall
        now_mono = time.monotonic()
        now_wall = time.time()

        # Cache collection for 15s; collector query timeout is 1s
        if now_mono - _last_collection_mono >= 15.0:
            sessionmaker = get_sessionmaker()
            try:
                async with sessionmaker() as session:
                    await asyncio.wait_for(refresh_operational_metrics(session), timeout=1.0)
            except Exception as exc:
                logger.error("Failed to collect operational metrics: %s", exc)
                age = (
                    (now_wall - _last_collection_wall)
                    if _last_collection_wall > 0
                    else (now_mono - _last_collection_mono)
                )
                METRICS_COLLECTION_AGE_SECONDS.set(max(age, 0.0))

        content = generate_latest(REGISTRY)
        return Response(content=content, media_type=CONTENT_TYPE_LATEST)
