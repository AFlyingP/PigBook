#!/usr/bin/env python3
"""Hold expiry test fixture seam (Spec 11.5, T-030).

Manipulates only the isolated test database configured by DATABASE_URL.
Fast-forwards the expires_at deadline for offered bookings and executes
the hold expiry and promotion scheduler service to advance queues.

Can be invoked directly or via:
  uv run --project backend python scripts/expire_hold.py [--resource-id UUID] [--booking-id UUID]
"""

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure backend directory is in sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from sqlalchemy import select, update, text
from app.db.session import get_sessionmaker
from app.bookings.models import Booking
from app.waitlist.models import WaitlistEntry
from app.waitlist.scheduler import expire_and_promote


async def run_expire_holds(
    resource_id: uuid.UUID | None = None,
    booking_id: uuid.UUID | None = None,
) -> int:
    sm = get_sessionmaker()
    now = datetime.now(timezone.utc)

    # 1. Update expires_at to the past in database
    target_resource_ids: list[uuid.UUID] = []

    async with sm() as session:
        async with session.begin():
            if booking_id is not None:
                b = await session.get(Booking, booking_id)
                if b is not None and b.resource_id is not None:
                    target_resource_ids.append(b.resource_id)
                await session.execute(
                    text(
                        "UPDATE bookings "
                        "SET expires_at = clock_timestamp() - interval '5 seconds' "
                        "WHERE id = :bid AND status = 'offered'"
                    ),
                    {"bid": booking_id},
                )
            elif resource_id is not None:
                target_resource_ids.append(resource_id)
                await session.execute(
                    text(
                        "UPDATE bookings "
                        "SET expires_at = clock_timestamp() - interval '5 seconds' "
                        "WHERE resource_id = :rid AND status = 'offered'"
                    ),
                    {"rid": resource_id},
                )
            else:
                stmt = select(Booking.resource_id).where(Booking.status == "offered").distinct()
                res = await session.execute(stmt)
                target_resource_ids = [r for r in res.scalars().all()]
                await session.execute(
                    text(
                        "UPDATE bookings "
                        "SET expires_at = clock_timestamp() - interval '5 seconds' "
                        "WHERE status = 'offered'"
                    )
                )

    # 2. Call expire_and_promote per resource to execute domain transition
    total_expired = 0
    for r_id in target_resource_ids:
        async with sm() as session:
            async with session.begin():
                count = await expire_and_promote(session, resource_id=r_id, now=now)
                total_expired += count

    print(f"Hold expiry seam completed: {total_expired} hold(s) expired across {len(target_resource_ids)} resource(s).")
    return total_expired


def main() -> None:
    parser = argparse.ArgumentParser(description="Hold expiry test fixture seam")
    parser.add_argument("--resource-id", type=uuid.UUID, default=None, help="Target resource UUID")
    parser.add_argument("--booking-id", type=uuid.UUID, default=None, help="Target offered booking UUID")

    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("Error: DATABASE_URL environment variable is required", file=sys.stderr)
        sys.exit(1)

    expired_count = asyncio.run(run_expire_holds(args.resource_id, args.booking_id))
    sys.exit(0)


if __name__ == "__main__":
    main()
