import asyncio
import json
import os
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError

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
@pytest.mark.parametrize("round_num", [1, 2, 3])
async def test_200_users_same_slot_race(base_url: str, round_num: int) -> None:
    """Spec 11.2: 200 distinct enabled member users attempt to book the exact same slot.

    Executed across 3 distinct rounds. Exactly one wins (201 Created), 199 conflict
    (409 Conflict with SLOT_CONFLICT). Database has exactly 1 booking, 200 completed keys,
    and 1 outbox event. SQLSTATE 23P01 evidence captured without leaking DB internals.
    """
    round_prefix = f"round_{round_num}"

    # 1. Authenticate / seed 200 distinct users outside the measured window
    users, tokens = await create_users(200, prefix=f"race_{round_prefix}")

    # 2. Distinct resource and time slot per round
    resource = await create_resource(name_prefix=f"Res_{round_prefix}")
    # Round 1: 10:00, Round 2: 12:00, Round 3: 14:00 (days_ahead = round_num)
    starts_at, ends_at = get_aligned_window(days_ahead=round_num, hour=10 + round_num * 2)

    payload = {
        "resource_id": str(resource.id),
        "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # 3. 200 distinct UUID v4 Idempotency-Keys
    keys = [str(uuid.uuid4()) for _ in range(200)]

    # 4. httpx.AsyncClient with pool max 250, keepalive 250, timeout 120s
    limits = httpx.Limits(max_connections=250, max_keepalive_connections=250)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=120.0) as client:
        barrier = asyncio.Event()

        async def contender(
            idx: int, token: str, key: str
        ) -> tuple[int, httpx.Response, float, float]:
            headers = {
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": key,
            }
            await barrier.wait()
            t_start = time.perf_counter()
            resp = await client.post("/api/v1/bookings", json=payload, headers=headers)
            t_finish = time.perf_counter()
            return idx, resp, t_start, t_finish

        tasks = [asyncio.create_task(contender(i, tokens[i], keys[i])) for i in range(200)]

        # Allow all coroutines to advance to the barrier
        await asyncio.sleep(0.05)

        t_release = time.perf_counter()
        barrier.set()

        results = await asyncio.gather(*tasks)

    # 5. Compute timing and skews
    start_skews = [r[2] - t_release for r in results]
    finish_times = [r[3] for r in results]
    total_duration = max(finish_times) - t_release

    # 6. Assert response status distribution: exactly {201: 1, 409: 199}
    statuses = [r[1].status_code for r in results]
    distribution = Counter(statuses)
    assert distribution == {201: 1, 409: 199}, (
        f"Round {round_num}: Expected {{201: 1, 409: 199}}, observed {dict(distribution)}"
    )

    # 7. Assert error contracts and no database internal leakage
    winner_response = None
    for _, resp, _, _ in results:
        if resp.status_code == 201:
            winner_response = resp
            assert resp.headers.get("idempotency-replayed") == "false"
            data = resp.json()
            assert "id" in data
            assert data["resource_id"] == str(resource.id)
            assert data["status"] == "confirmed"
        elif resp.status_code == 409:
            data = resp.json()
            assert "error" in data
            assert data["error"]["code"] == "SLOT_CONFLICT"
            # Invariant: No database internals (SQLSTATE, constraint name, table name) in body
            body_text = resp.text
            assert "23P01" not in body_text, "SQLSTATE 23P01 leaked into HTTP response body"
            assert "bookings_no_overlap" not in body_text, (
                "Constraint name leaked into HTTP response body"
            )
            assert "relation" not in body_text.lower(), (
                "Database table internal leaked into HTTP body"
            )

    assert winner_response is not None, "No winner (201 Created) observed"
    winner_id = uuid.UUID(winner_response.json()["id"])

    # 8. Assert database state
    sm = get_sessionmaker()
    async with sm() as session:
        # Exactly ONE active booking
        confirmed_count = await session.scalar(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.resource_id == resource.id,
                Booking.status == "confirmed",
            )
        )
        assert confirmed_count == 1, (
            f"Round {round_num}: Expected exactly 1 confirmed booking, found {confirmed_count}"
        )

        # Exactly 200 completed idempotency rows
        uuid_keys = [uuid.UUID(k) for k in keys]
        completed_idemp_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(
                IdempotencyKey.key.in_(uuid_keys),
                IdempotencyKey.response_status.is_not(None),
            )
        )
        assert completed_idemp_count == 200, (
            f"Round {round_num}: Expected 200 completed rows, found {completed_idemp_count}"
        )

        # Exactly ONE booking_confirmed outbox event
        outbox_count = await session.scalar(
            select(func.count())
            .select_from(Outbox)
            .where(
                Outbox.aggregate_id == winner_id,
                Outbox.event_type == "booking_confirmed",
            )
        )
        assert outbox_count == 1, (
            f"Round {round_num}: Expected exactly 1 outbox event for winner, found {outbox_count}"
        )

        # 9. Direct execution probe to capture actual driver SQLSTATE and constraint_name
        # Seed a distinct user for the probe
        probe_users, _ = await create_users(1, prefix=f"probe_{round_prefix}")
        probe_user = probe_users[0]
        captured_sqlstate = None
        captured_constraint = None

        try:
            async with session.begin_nested():
                probe_booking = Booking(
                    id=uuid.uuid4(),
                    resource_id=resource.id,
                    user_id=probe_user.id,
                    created_by=probe_user.id,
                    kind="reservation",
                    time_range=Range(starts_at, ends_at, bounds="[)"),
                    status="confirmed",
                    expires_at=None,
                    version=1,
                )
                session.add(probe_booking)
                await session.flush()
        except IntegrityError as exc:
            orig = getattr(exc, "orig", exc)
            driver_exc = getattr(orig, "__cause__", None) or orig
            captured_sqlstate = (
                getattr(driver_exc, "sqlstate", None)
                or getattr(orig, "sqlstate", None)
                or getattr(orig, "pgcode", None)
            )
            captured_constraint = getattr(driver_exc, "constraint_name", None) or getattr(
                orig, "constraint_name", None
            )

        assert captured_sqlstate == "23P01", (
            f"Round {round_num}: Expected driver SQLSTATE 23P01, captured {captured_sqlstate}"
        )
        assert captured_constraint == "bookings_no_overlap", (
            f"Round {round_num}: Expected bookings_no_overlap, captured {captured_constraint}"
        )

        # Confirm probe rollback did not alter active booking count
        post_probe_count = await session.scalar(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.resource_id == resource.id,
                Booking.status == "confirmed",
            )
        )
        assert post_probe_count == 1, (
            f"Round {round_num}: Probe altered booking count to {post_probe_count}"
        )

    # 10. Structure and log evidence using captured driver values
    evidence = {
        "gate": "concurrency",
        "test": "test_same_slot.py",
        "round": round_num,
        "resource_id": str(resource.id),
        "slot": {
            "starts_at": payload["starts_at"],
            "ends_at": payload["ends_at"],
        },
        "contenders": 200,
        "status_distribution": dict(distribution),
        "duration_seconds": round(total_duration, 4),
        "start_skew_ms": {
            "min": round(min(start_skews) * 1000, 2),
            "max": round(max(start_skews) * 1000, 2),
            "avg": round((sum(start_skews) / len(start_skews)) * 1000, 2),
        },
        "sqlstate_evidence": {
            "observed_error_code": "SLOT_CONFLICT",
            "captured_driver_sqlstate": captured_sqlstate,
            "captured_constraint_name": captured_constraint,
            "leaked_in_http_body": False,
        },
        "git_sha": get_git_sha(),
    }

    print(f"\n[CONCURRENCY RACE EVIDENCE] Round {round_num}: {json.dumps(evidence)}")

    evidence_dir = os.environ.get("EVIDENCE_DIR")
    if evidence_dir:
        ev_path = Path(evidence_dir) / f"concurrency_round_{round_num}.json"
        ev_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
