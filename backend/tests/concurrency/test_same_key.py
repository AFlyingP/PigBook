import asyncio
import json
import os
import subprocess
import time
import uuid
from collections import Counter
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text

from app.bookings.models import Booking, IdempotencyKey
from app.db.session import get_sessionmaker
from app.notifications.models import Outbox
from tests.concurrency.conftest import (
    create_resource,
    create_users,
    get_aligned_window,
)


def get_git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return proc.stdout.strip()
    except Exception:
        return "unknown"


@pytest.mark.asyncio
async def test_200_same_user_same_key_concurrent(base_url: str) -> None:
    """Spec 11.2: 200 identical concurrent requests from the same user with the same key.

    All 200 return 201 with identical body and booking ID. Exactly one has
    Idempotency-Replayed: false; 199 have Idempotency-Replayed: true.
    Database has exactly 1 booking row, 1 idempotency key row, and 1 outbox event.
    Requires TEST_PROFILE=race (1000/user/min limit) to process 200 requests from 1 user.
    """
    _users, tokens = await create_users(1, prefix="same_key_200")
    token = tokens[0]
    resource = await create_resource(name_prefix="Res_SameKey")
    starts_at, ends_at = get_aligned_window(days_ahead=4, hour=10)

    payload = {
        "resource_id": str(resource.id),
        "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    shared_key = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": shared_key,
    }

    limits = httpx.Limits(max_connections=250, max_keepalive_connections=250)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=120.0) as client:
        barrier = asyncio.Event()

        async def send_req(idx: int) -> tuple[int, httpx.Response, float, float]:
            await barrier.wait()
            t_start = time.perf_counter()
            resp = await client.post("/api/v1/bookings", json=payload, headers=headers)
            t_finish = time.perf_counter()
            return idx, resp, t_start, t_finish

        tasks = [asyncio.create_task(send_req(i)) for i in range(200)]
        await asyncio.sleep(0.05)
        t_release = time.perf_counter()
        barrier.set()

        results = await asyncio.gather(*tasks)

    # Timing metrics
    start_skews = [r[2] - t_release for r in results]
    finish_times = [r[3] for r in results]
    total_duration = max(finish_times) - t_release

    # 1. Assert all 200 are 201 Created
    statuses = [r[1].status_code for r in results]
    assert Counter(statuses) == {201: 200}, f"Expected all 201, observed {Counter(statuses)}"

    # 2. Assert exactly one false and 199 true for Idempotency-Replayed
    replayed_flags = [r[1].headers.get("idempotency-replayed") for r in results]
    assert Counter(replayed_flags) == {"false": 1, "true": 199}

    # 3. Assert all 200 bodies have the exact same booking ID and payload
    first_body = results[0][1].json()
    booking_id = first_body["id"]
    for _, resp in results:
        assert resp.json() == first_body

    # 4. Assert database persistence: exactly 1 booking, 1 key, 1 outbox
    sm = get_sessionmaker()
    async with sm() as session:
        b_count = await session.scalar(
            select(func.count()).select_from(Booking).where(Booking.id == uuid.UUID(booking_id))
        )
        assert b_count == 1

        k_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key == uuid.UUID(shared_key))
        )
        assert k_count == 1

        o_count = await session.scalar(
            select(func.count())
            .select_from(Outbox)
            .where(
                Outbox.aggregate_id == uuid.UUID(booking_id),
                Outbox.event_type == "booking_confirmed",
            )
        )
        assert o_count == 1

    # 5. Structure and emit machine-readable same-key concurrency artifact
    evidence = {
        "gate": "concurrency",
        "test": "test_same_key.py",
        "contenders": 200,
        "shared_idempotency_key": shared_key,
        "booking_id": booking_id,
        "status_distribution": dict(Counter(statuses)),
        "replay_distribution": dict(Counter(replayed_flags)),
        "database_counts": {
            "booking_count": b_count,
            "idempotency_key_count": k_count,
            "outbox_count": o_count,
        },
        "duration_seconds": round(total_duration, 4),
        "start_skew_ms": {
            "min": round(min(start_skews) * 1000, 2),
            "max": round(max(start_skews) * 1000, 2),
            "avg": round((sum(start_skews) / len(start_skews)) * 1000, 2),
        },
        "git_sha": get_git_sha(),
    }

    print(f"\n[SAME KEY CONCURRENCY EVIDENCE]: {json.dumps(evidence)}")

    evidence_dir = os.environ.get("EVIDENCE_DIR")
    if evidence_dir:
        ev_path = Path(evidence_dir) / "concurrency_same_key.json"
        ev_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_same_key_changed_payload_concurrent(base_url: str) -> None:
    """Spec 11.2: Same key sent with different payloads concurrently.

    One owns the row and commits 201; the other blocks on the row lock and fails 422.
    """
    _users, tokens = await create_users(1, prefix="diff_payload")
    token = tokens[0]
    res1 = await create_resource(name_prefix="Res_Diff1")
    res2 = await create_resource(name_prefix="Res_Diff2")

    s1, e1 = get_aligned_window(days_ahead=5, hour=10)
    s2, e2 = get_aligned_window(days_ahead=5, hour=12)

    payload_a = {
        "resource_id": str(res1.id),
        "starts_at": s1.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e1.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload_b = {
        "resource_id": str(res2.id),
        "starts_at": s2.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e2.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    shared_key = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": shared_key,
    }

    limits = httpx.Limits(max_connections=20, max_keepalive_connections=20)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=60.0) as client:
        barrier = asyncio.Event()

        async def send(payload: dict) -> httpx.Response:
            await barrier.wait()
            return await client.post("/api/v1/bookings", json=payload, headers=headers)

        task_a = asyncio.create_task(send(payload_a))
        task_b = asyncio.create_task(send(payload_b))
        await asyncio.sleep(0.05)
        barrier.set()

        r_a, r_b = await asyncio.gather(task_a, task_b)

    status_codes = sorted([r_a.status_code, r_b.status_code])
    assert status_codes == [201, 422]

    resp_422 = r_a if r_a.status_code == 422 else r_b
    assert resp_422.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


@pytest.mark.asyncio
async def test_lost_response_replay_after_commit(base_url: str) -> None:
    """Spec 5.4 / 11.2: Replay after successful commit returns identical stored response."""
    _users, tokens = await create_users(1, prefix="lost_resp")
    token = tokens[0]
    resource = await create_resource(name_prefix="Res_LostResp")
    starts_at, ends_at = get_aligned_window(days_ahead=6, hour=10)

    payload = {
        "resource_id": str(resource.id),
        "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    key = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # Initial call commits and returns response
        r1 = await client.post("/api/v1/bookings", json=payload, headers=headers)
        assert r1.status_code == 201
        assert r1.headers.get("idempotency-replayed") == "false"
        data1 = r1.json()

        # Replay (e.g. client dropped network and re-requested with same key)
        r2 = await client.post("/api/v1/bookings", json=payload, headers=headers)
        assert r2.status_code == 201
        assert r2.headers.get("idempotency-replayed") == "true"
        assert r2.json() == data1
        assert r2.headers.get("Location") == r1.headers.get("Location")
        assert r2.headers.get("ETag") == r1.headers.get("ETag")


@pytest.mark.asyncio
async def test_cached_409_replay_after_capacity_frees(base_url: str) -> None:
    """Spec 5.2 / 11.2: Cached 409 conflict persists on replay even after capacity frees.

    A new key is required to obtain the newly-available slot.
    """
    _users, tokens = await create_users(2, prefix="c409")
    t1, t2 = tokens[0], tokens[1]
    resource = await create_resource(name_prefix="Res_C409")

    s, e = get_aligned_window(days_ahead=7, hour=14)
    payload = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # User 1 claims slot S
        k1 = str(uuid.uuid4())
        r1 = await client.post(
            "/api/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {t1}", "Idempotency-Key": k1},
        )
        assert r1.status_code == 201
        booking_1_id = uuid.UUID(r1.json()["id"])

        # User 2 attempts slot S -> 409 SLOT_CONFLICT
        k2 = str(uuid.uuid4())
        r2 = await client.post(
            "/api/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {t2}", "Idempotency-Key": k2},
        )
        assert r2.status_code == 409
        assert r2.headers.get("idempotency-replayed") == "false"
        assert r2.json()["error"]["code"] == "SLOT_CONFLICT"

        # Cancel User 1's booking directly in database to free capacity
        sm = get_sessionmaker()
        async with sm() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE bookings SET status = 'cancelled', "
                        "version = version + 1 WHERE id = :id"
                    ),
                    {"id": booking_1_id},
                )

        # User 2 replays with key k2 -> cached 409 MUST still be returned
        r2_replay = await client.post(
            "/api/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {t2}", "Idempotency-Key": k2},
        )
        assert r2_replay.status_code == 409
        assert r2_replay.headers.get("idempotency-replayed") == "true"
        assert r2_replay.json()["error"]["code"] == "SLOT_CONFLICT"

        # User 2 sends NEW key k3 -> succeeds with 201
        k3 = str(uuid.uuid4())
        r2_fresh = await client.post(
            "/api/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {t2}", "Idempotency-Key": k3},
        )
        assert r2_fresh.status_code == 201
        assert r2_fresh.headers.get("idempotency-replayed") == "false"


@pytest.mark.asyncio
async def test_explicit_key_expiry_at_24h_boundary(base_url: str) -> None:
    """Spec 4.3 / 11.2: Key at expires_at <= database clock is deleted and reusable as fresh."""
    _users, tokens = await create_users(1, prefix="expiry_24h")
    token = tokens[0]
    resource = await create_resource(name_prefix="Res_Expiry")

    s1, e1 = get_aligned_window(days_ahead=8, hour=10)
    s2, e2 = get_aligned_window(days_ahead=8, hour=12)

    payload1 = {
        "resource_id": str(resource.id),
        "starts_at": s1.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e1.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload2 = {
        "resource_id": str(resource.id),
        "starts_at": s2.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e2.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    key = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # 1. Create booking with key
        r1 = await client.post("/api/v1/bookings", json=payload1, headers=headers)
        assert r1.status_code == 201
        first_id = r1.json()["id"]

        # 2. Recheck before expiry (e.g. 5 minutes before) -> still replays
        sm = get_sessionmaker()
        async with sm() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE idempotency_keys "
                        "SET created_at = sampled.at, "
                        "    expires_at = sampled.at + interval '24 hours' "
                        "FROM (SELECT clock_timestamp() - interval '23 hours 55 minutes' AS at) "
                        "     AS sampled "
                        "WHERE key = :key"
                    ),
                    {"key": uuid.UUID(key)},
                )

        r_unexpired = await client.post("/api/v1/bookings", json=payload1, headers=headers)
        assert r_unexpired.status_code == 201
        assert r_unexpired.headers.get("idempotency-replayed") == "true"
        assert r_unexpired.json()["id"] == first_id

        # 3. Fast-forward past 24h (expires_at <= clock_timestamp())
        async with sm() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE idempotency_keys "
                        "SET created_at = sampled.at, "
                        "    expires_at = sampled.at + interval '24 hours' "
                        "FROM (SELECT clock_timestamp() - interval '24 hours 10 seconds' AS at) "
                        "     AS sampled "
                        "WHERE key = :key"
                    ),
                    {"key": uuid.UUID(key)},
                )

        # 4. Send request with expired key -> expired row deleted -> executed fresh
        r_expired = await client.post("/api/v1/bookings", json=payload2, headers=headers)
        assert r_expired.status_code == 201
        assert r_expired.headers.get("idempotency-replayed") == "false"
        assert r_expired.json()["id"] != first_id


@pytest.mark.asyncio
async def test_failed_transaction_leaves_no_incomplete_key(base_url: str) -> None:
    """Spec 4.3 / 5.2: Failed transaction rolls back and leaves no idempotency row."""
    _users, tokens = await create_users(1, prefix="failed_tx")
    token = tokens[0]

    # Non-existent resource ID (triggers 404 NOT_FOUND)
    s, e = get_aligned_window(days_ahead=9, hour=10)
    payload_404 = {
        "resource_id": str(uuid.uuid4()),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    key_404 = str(uuid.uuid4())

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        r_404 = await client.post(
            "/api/v1/bookings",
            json=payload_404,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key_404},
        )
        assert r_404.status_code == 404

    # Assert 0 rows in idempotency_keys
    sm = get_sessionmaker()
    async with sm() as session:
        k_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key == uuid.UUID(key_404))
        )
        assert k_count == 0

    # Invalid window: duration 5 hours > 4 hours (triggers 422 INVALID_WINDOW)
    resource = await create_resource(name_prefix="Res_Fail")
    payload_422 = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": (s + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    key_422 = str(uuid.uuid4())

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        r_422 = await client.post(
            "/api/v1/bookings",
            json=payload_422,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key_422},
        )
        assert r_422.status_code == 422

    async with sm() as session:
        k_count_422 = await session.scalar(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key == uuid.UUID(key_422))
        )
        assert k_count_422 == 0
