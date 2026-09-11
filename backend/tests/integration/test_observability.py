import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import text

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")
os.environ.setdefault("METRICS_TOKEN", "test-metrics-token-minimum-32-bytes-long-1234")

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.notifications.models import Outbox, WorkerHeartbeat
from app.observability.logging import (
    StructuredJsonFormatter,
    redact_sensitive_text,
)
from app.observability.metrics import (
    refresh_operational_metrics,
)


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> None:
    yield
    import app.db.session

    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:5173",
    )


@pytest.mark.asyncio
async def test_metrics_auth_absent_wrong_and_correct_token() -> None:
    """Metrics endpoint rejects missing and invalid tokens with 401, admits valid token with 200."""
    valid_token = get_settings().METRICS_TOKEN or "test-metrics-token-minimum-32-bytes-long-1234"

    async with make_client() as client:
        # 1. Absent token -> 401
        r_absent = await client.get("/metrics")
        assert r_absent.status_code == 401
        assert r_absent.json()["error"]["code"] == "AUTH_REQUIRED"

        # 2. Wrong token -> 401
        r_wrong = await client.get(
            "/metrics", headers={"Authorization": "Bearer wrong-token-value"}
        )
        assert r_wrong.status_code == 401
        assert r_wrong.json()["error"]["code"] == "INVALID_TOKEN"

        # 3. Non-ASCII token -> 401 INVALID_TOKEN, not 500 (R2)
        r_non_ascii = await client.get(
            "/metrics", headers=[(b"authorization", b"Bearer \xff\xfe-invalid-token")]
        )
        assert r_non_ascii.status_code == 401
        assert r_non_ascii.json()["error"]["code"] == "INVALID_TOKEN"

        # 4. Correct token -> 200 Prometheus text
        r_correct = await client.get("/metrics", headers={"Authorization": f"Bearer {valid_token}"})
        assert r_correct.status_code == 200
        assert "text/plain" in r_correct.headers.get("content-type", "")
        assert "commonsbook_http_requests_total" in r_correct.text


@pytest.mark.asyncio
async def test_exact_metric_names_labels_and_histogram_buckets() -> None:
    """Exact Spec 10.1 metric names, types, labels and duration buckets are registered."""
    metrics_text = generate_latest(REGISTRY).decode("utf-8")

    import re

    from app.observability.metrics import HTTP_REQUEST_DURATION_SECONDS

    # 1. Assert exact metric family and exact # TYPE from Prometheus exposition
    expected_types = {
        "commonsbook_http_requests_total": "counter",
        "commonsbook_http_request_duration_seconds": "histogram",
        "commonsbook_booking_conflicts_total": "counter",
        "commonsbook_idempotency_total": "counter",
        "commonsbook_outbox_pending": "gauge",
        "commonsbook_outbox_lag_seconds": "gauge",
        "commonsbook_outbox_dead": "gauge",
        "commonsbook_outbox_deliveries_total": "counter",
        "commonsbook_expired_holds_pending": "gauge",
        "commonsbook_worker_heartbeat_age_seconds": "gauge",
        "commonsbook_db_pool_checked_out": "gauge",
        "commonsbook_metrics_collection_age_seconds": "gauge",
    }

    for name, mtype in expected_types.items():
        assert f"# TYPE {name} {mtype}" in metrics_text, f"Missing or incorrect # TYPE for {name}"

    # 2. Assert exact label-name set for every labeled metric
    expected_labels = {
        "commonsbook_http_requests_total": {"method", "route", "status_class"},
        "commonsbook_http_request_duration_seconds": {"method", "route"},
        "commonsbook_booking_conflicts_total": {"operation"},
        "commonsbook_idempotency_total": {"outcome"},
        "commonsbook_outbox_deliveries_total": {"outcome"},
    }
    collector_map = REGISTRY._names_to_collectors
    for name, expected_lbls in expected_labels.items():
        collector_key = (
            name[:-6] if (name.endswith("_total") and name not in collector_map) else name
        )
        assert collector_key in collector_map, f"Collector not registered for {name}"
        actual_lbls = set(collector_map[collector_key]._labelnames)
        assert actual_lbls == expected_lbls, (
            f"Label set mismatch for {name}: {actual_lbls} != {expected_lbls}"
        )

    # 3. Assert exact commonsbook_http_request_duration_seconds_bucket le boundaries including +Inf
    HTTP_REQUEST_DURATION_SECONDS.labels(method="GET", route="/healthz").observe(0.01)
    updated_text = generate_latest(REGISTRY).decode("utf-8")
    bucket_le_values = set(
        re.findall(
            r'commonsbook_http_request_duration_seconds_bucket\{[^}]*le="([^"]+)"[^}]*\}',
            updated_text,
        )
    )
    expected_le_boundaries = {
        "0.005",
        "0.01",
        "0.025",
        "0.05",
        "0.1",
        "0.25",
        "0.5",
        "1.0",
        "2.5",
        "5.0",
        "10.0",
        "30.0",
        "+Inf",
    }
    assert bucket_le_values == expected_le_boundaries, (
        f"Duration bucket boundaries mismatch: {bucket_le_values}"
    )


@pytest.mark.asyncio
async def test_no_high_cardinality_labels() -> None:
    """Metric labels must never contain high-cardinality values such as UUIDs or user IDs."""
    valid_token = get_settings().METRICS_TOKEN or "test-metrics-token-minimum-32-bytes-long-1234"

    # Make a few diverse requests with UUIDs in path and headers
    async with make_client() as client:
        test_uuid = str(uuid.uuid4())
        await client.get(
            f"/api/v1/resources/{test_uuid}", headers={"X-Request-ID": str(uuid.uuid4())}
        )
        await client.get("/healthz", headers={"X-Request-ID": str(uuid.uuid4())})
        await client.get("/metrics", headers={"Authorization": f"Bearer {valid_token}"})

    metrics_text = generate_latest(REGISTRY).decode("utf-8")

    for line in metrics_text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        # Check label keys and values
        if "{" in line:
            label_part = line[line.find("{") + 1 : line.find("}")]
            # Labels must not contain 'id', 'user', 'resource', 'request_id' as label names
            for item in label_part.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"')
                    assert k not in ("user_id", "resource_id", "request_id", "booking_id"), (
                        f"Forbidden label name {k}"
                    )
                    # Label values must not be a raw UUID
                    assert not (len(v) == 36 and v.count("-") == 4), (
                        f"UUID leaked in label value: {v}"
                    )


@pytest.mark.asyncio
async def test_db_down_readiness_returns_503() -> None:
    """When the database is unreachable, /readyz returns 503 without leaking DB internals."""
    with patch("app.main.get_sessionmaker") as mock_sm:
        mock_session = AsyncMock()
        mock_session.__aenter__.side_effect = Exception(
            "connection to server at 127.0.0.1:5432 failed: Connection refused"
        )
        mock_sm.return_value = mock_session

        async with make_client() as client:
            r = await client.get("/readyz")
            assert r.status_code == 503
            assert r.headers.get("retry-after") == "1"
            err = r.json()["error"]
            assert err["code"] == "RETRYABLE_UNAVAILABLE"
            assert err["message"] == "Service unavailable"
            assert "connection refused" not in str(err).lower()
            assert "5432" not in str(err)


def test_structured_log_secret_redaction() -> None:
    """Structured logs must redact Authorization, Cookie, password, and invitation tokens."""
    sensitive_log = (
        '{"Authorization": "Bearer secret_token_abc_123", "Cookie": "session=xyz987", '
        '"password": "super_secret_password", "invitation_token": "secret_invite_456"}'
    )
    redacted = redact_sensitive_text(sensitive_log)
    assert "secret_token_abc_123" not in redacted
    assert "xyz987" not in redacted
    assert "super_secret_password" not in redacted
    assert "secret_invite_456" not in redacted
    assert "[REDACTED]" in redacted


def test_structured_log_formatter_exact_fields() -> None:
    """StructuredJsonFormatter emits single-line JSON with strictly the 14 defined fields."""
    formatter = StructuredJsonFormatter(service="api")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="test_event",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    expected_keys = [
        "timestamp",
        "level",
        "service",
        "release_sha",
        "event",
        "request_id",
        "route",
        "method",
        "status_code",
        "duration_ms",
        "trace_id",
        "error_code",
        "outbox_id",
        "attempt",
    ]
    assert list(parsed.keys()) == expected_keys
    assert parsed["level"] == "INFO"
    assert parsed["service"] == "api"


@pytest.mark.asyncio
async def test_staleness_indicator_on_collector_failure() -> None:
    """Collector failure logs and leaves staleness indicator metrics_collection_age_seconds."""
    valid_token = get_settings().METRICS_TOKEN or "test-metrics-token-minimum-32-bytes-long-1234"

    # Reset collection time so that next request attempts collection
    import app.observability.metrics as metrics_mod

    metrics_mod._last_collection_mono = time.monotonic() - 100.0
    metrics_mod._last_collection_wall = time.time() - 120.0

    # Simulate DB error during operational collection
    with patch(
        "app.observability.metrics.refresh_operational_metrics",
        side_effect=RuntimeError("Collector DB timeout"),
    ):
        async with make_client() as client:
            r = await client.get("/metrics", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            # Check that staleness indicator is >= 60 seconds
            metrics_map = {m.name: m for m in REGISTRY.collect()}
            stale_sample = [
                s.value
                for s in metrics_map["commonsbook_metrics_collection_age_seconds"].samples
                if s.name == "commonsbook_metrics_collection_age_seconds"
            ][0]
            assert stale_sample >= 60.0


@pytest.mark.asyncio
async def test_dead_worker_and_outbox_synthetic_alert_conditions() -> None:
    """Verify gauge metrics reflect conditions that trigger alerts (dead outbox, dead worker)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Seed an outbox dead event
        async with session.begin():
            # Clean any existing outbox/heartbeat rows
            await session.execute(text("DELETE FROM notification_deliveries"))
            await session.execute(text("DELETE FROM outbox"))
            await session.execute(text("DELETE FROM worker_heartbeat"))

            from sqlalchemy.dialects.postgresql import Range

            from app.auth.models import User
            from app.auth.passwords import hash_password
            from app.bookings.models import Booking
            from app.resources.models import Resource

            test_user = User(
                id=uuid.uuid4(),
                email=f"worker_test_{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("ValidPassword123!"),
                display_name="Worker Test User",
                role="admin",
                enabled=True,
                version=1,
            )
            session.add(test_user)

            test_res = Resource(
                id=uuid.uuid4(),
                name=f"WorkerTestRes_{uuid.uuid4().hex[:6]}",
                description="Resource for dead worker test",
                location="Room 101",
                active=True,
                version=1,
            )
            session.add(test_res)
            await session.flush()

            # Create dead outbox row
            bkg_id = uuid.uuid4()
            bkg = Booking(
                id=bkg_id,
                resource_id=test_res.id,
                user_id=test_user.id,
                created_by=test_user.id,
                kind="reservation",
                time_range=Range(
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc) + timedelta(hours=1),
                    bounds="[)",
                ),
                status="confirmed",
                version=1,
            )
            session.add(bkg)
            await session.flush()

            dead_event = Outbox(
                id=uuid.uuid4(),
                event_type="booking_confirmed",
                aggregate_id=bkg_id,
                aggregate_version=1,
                payload={"test": "data"},
                status="dead",
                attempts=5,
                occurred_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            )
            session.add(dead_event)

            # Worker heartbeat dead/stale (seen 3 minutes ago, alert threshold 90s)
            stale_hb = WorkerHeartbeat(
                name="primary",
                seen_at=datetime.now(timezone.utc) - timedelta(seconds=180),
                expiry_scan_at=datetime.now(timezone.utc) - timedelta(seconds=180),
            )
            session.add(stale_hb)

        # Refresh metrics
        await refresh_operational_metrics(session)

        # Verify dead outbox gauge > 0 (triggers OutboxDeadEvents)
        metrics_map = {m.name: m for m in REGISTRY.collect()}
        dead_val = [
            s.value
            for s in metrics_map["commonsbook_outbox_dead"].samples
            if s.name == "commonsbook_outbox_dead"
        ][0]
        assert dead_val >= 1.0

        # Verify worker heartbeat age > 90s (triggers WorkerHeartbeatStale)
        hb_val = [
            s.value
            for s in metrics_map["commonsbook_worker_heartbeat_age_seconds"].samples
            if s.name == "commonsbook_worker_heartbeat_age_seconds"
        ][0]
        assert hb_val >= 90.0

        # Clean up seeded test rows
        async with sessionmaker() as cleanup_session:
            async with cleanup_session.begin():
                await cleanup_session.execute(text("DELETE FROM outbox"))
                await cleanup_session.execute(text("DELETE FROM worker_heartbeat"))
                await cleanup_session.execute(
                    text("DELETE FROM bookings WHERE id = :bid"), {"bid": bkg_id}
                )
                await cleanup_session.execute(
                    text("DELETE FROM resources WHERE id = :rid"), {"rid": test_res.id}
                )
                await cleanup_session.execute(
                    text("DELETE FROM users WHERE id = :uid"), {"uid": test_user.id}
                )


@pytest.mark.asyncio
async def test_outbox_delivery_counter_restart_safety() -> None:
    """First collection establishes baseline without incrementing; subsequent deliveries do."""
    from sqlalchemy.dialects.postgresql import Range

    import app.observability.metrics as metrics_mod
    from app.auth.models import User
    from app.auth.passwords import hash_password
    from app.bookings.models import Booking
    from app.notifications.models import NotificationDelivery, Outbox
    from app.observability.metrics import OUTBOX_DELIVERIES_TOTAL, refresh_operational_metrics
    from app.resources.models import Resource

    # Reset module-level baseline state to simulate a fresh process start
    metrics_mod._delivery_baseline_established = False
    metrics_mod._last_delivery_counts = {"sent": 0, "skipped": 0}

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM notification_deliveries"))
            await session.execute(text("DELETE FROM outbox"))

            u = User(
                id=uuid.uuid4(),
                email=f"u_{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("Pass12345678!"),
                display_name="U",
                role="member",
                enabled=True,
                version=1,
            )
            session.add(u)
            r = Resource(
                id=uuid.uuid4(),
                name=f"R_{uuid.uuid4().hex[:6]}",
                description="",
                location="L",
                active=True,
                version=1,
            )
            session.add(r)
            await session.flush()

            b = Booking(
                id=uuid.uuid4(),
                resource_id=r.id,
                user_id=u.id,
                created_by=u.id,
                kind="reservation",
                time_range=Range(
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc) + timedelta(hours=1),
                    bounds="[)",
                ),
                status="confirmed",
                version=1,
            )
            session.add(b)
            await session.flush()

            e1 = Outbox(
                id=uuid.uuid4(),
                event_type="booking_confirmed",
                aggregate_id=b.id,
                aggregate_version=1,
                payload={},
                status="delivered",
            )
            e2 = Outbox(
                id=uuid.uuid4(),
                event_type="booking_cancelled",
                aggregate_id=b.id,
                aggregate_version=2,
                payload={},
                status="delivered",
            )
            session.add_all([e1, e2])
            await session.flush()

            d1 = NotificationDelivery(
                id=uuid.uuid4(),
                event_id=e1.id,
                recipient_id=u.id,
                channel="email",
                state="sent",
            )
            d2 = NotificationDelivery(
                id=uuid.uuid4(),
                event_id=e2.id,
                recipient_id=u.id,
                channel="email",
                state="skipped",
            )
            session.add_all([d1, d2])

        def get_counter_value(state: str) -> float:
            for sample in OUTBOX_DELIVERIES_TOTAL.collect()[0].samples:
                if sample.labels.get("outcome") == state and sample.name.endswith("_total"):
                    return sample.value
            return 0.0

        initial_sent = get_counter_value("sent")
        initial_skipped = get_counter_value("skipped")

        # 1. First collection after restart -> must establish baseline without incrementing
        await refresh_operational_metrics(session)
        assert get_counter_value("sent") == initial_sent
        assert get_counter_value("skipped") == initial_skipped

        # 2. Add one new delivery in fresh transaction
        async with sessionmaker() as step2_session:
            async with step2_session.begin():
                e3 = Outbox(
                    id=uuid.uuid4(),
                    event_type="waitlist_offered",
                    aggregate_id=b.id,
                    aggregate_version=3,
                    payload={},
                    status="delivered",
                )
                step2_session.add(e3)
                await step2_session.flush()

                d3 = NotificationDelivery(
                    id=uuid.uuid4(),
                    event_id=e3.id,
                    recipient_id=u.id,
                    channel="email",
                    state="sent",
                )
                step2_session.add(d3)

            # Second collection -> must increment by exactly 1
            await refresh_operational_metrics(step2_session)
        assert get_counter_value("sent") == initial_sent + 1.0
        assert get_counter_value("skipped") == initial_skipped

        # Cleanup
        async with sessionmaker() as cleanup_session:
            async with cleanup_session.begin():
                await cleanup_session.execute(text("DELETE FROM notification_deliveries"))
                await cleanup_session.execute(text("DELETE FROM outbox"))
                await cleanup_session.execute(
                    text("DELETE FROM bookings WHERE id = :bid"), {"bid": b.id}
                )
                await cleanup_session.execute(
                    text("DELETE FROM resources WHERE id = :rid"), {"rid": r.id}
                )
                await cleanup_session.execute(
                    text("DELETE FROM users WHERE id = :uid"), {"uid": u.id}
                )
