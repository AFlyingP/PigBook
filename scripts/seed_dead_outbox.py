#!/usr/bin/env python3
"""Dead outbox test fixture seam (Spec 11.5).

Manipulates only the isolated test database configured by DATABASE_URL.
Ensures at least one dead-lettered outbox entry exists for testing the
administrator dead-item retry flow (Spec 4.2 E31, E32; T-032).

Can be invoked directly or via:
  uv run --project backend python scripts/seed_dead_outbox.py
"""

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.bookings.models import Booking
from app.db.session import get_sessionmaker
from app.notifications.models import Outbox
from sqlalchemy import select, text


async def run_seed_dead_outbox() -> str:
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        # Check if any dead outbox row already exists
        existing_dead = await session.execute(
            select(Outbox).where(Outbox.status == "dead").limit(1)
        )
        dead_row = existing_dead.scalar_one_or_none()
        if dead_row is not None:
            print(f"Existing dead outbox row found: {dead_row.id}")
            return str(dead_row.id)

        # Check if an existing outbox row can be marked dead
        any_outbox = await session.execute(select(Outbox).limit(1))
        row = any_outbox.scalar_one_or_none()
        if row is not None:
            row.status = "dead"
            row.attempts = 5
            row.last_error = "Simulated delivery failure"
            await session.flush()
            print(f"Updated outbox row {row.id} to dead status.")
            return str(row.id)

        # Otherwise find an existing booking to link a new dead outbox row to
        stmt = select(Booking).limit(1)
        booking = (await session.execute(stmt)).scalar_one_or_none()
        if booking is None:
            raise RuntimeError("Cannot seed dead outbox: no booking exists in database")

        new_outbox = Outbox(
            id=uuid.uuid4(),
            event_type="booking_confirmed",
            aggregate_id=booking.id,
            aggregate_version=1,
            payload={"booking_id": str(booking.id)},
            status="dead",
            attempts=5,
            last_error="Simulated delivery failure",
        )
        session.add(new_outbox)
        await session.flush()
        print(f"Created new dead outbox row: {new_outbox.id}")
        return str(new_outbox.id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dead outbox test fixture seam")
    parser.parse_args()
    asyncio.run(run_seed_dead_outbox())


if __name__ == "__main__":
    main()
