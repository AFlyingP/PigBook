"""Notification event dispatcher handler (Spec 3.4, 6.1, 6.3, 8.3)."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import User
from app.bookings.models import Booking
from app.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.notifications.adapters import EmailAdapter, EmailDeliveryError, EmailMessage
from app.notifications.models import NotificationDelivery
from app.notifications.outbox import OutboxLease, acknowledge, fail_lease
from app.notifications.templates import (
    render_booking_cancelled,
    render_booking_confirmed,
    render_hold_expired,
    render_waitlist_offered,
)
from app.resources.models import Resource

logger = logging.getLogger("notifications.handler")


async def dispatch_event(
    lease: OutboxLease,
    adapter: EmailAdapter,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> None:
    """Dispatch an outbox event lease to the notification email adapter.

    1. Get-or-create the notification_deliveries receipt keyed by (event, recipient, email).
       If a sent or skipped receipt exists, skips sending and marks outbox delivered.
    2. Check recipient status: disabled recipient -> skipped.
    3. Check waitlist_offered: if linked booking is no longer offered -> skipped.
    4. Release database connection.
    5. Send email outside transaction with stable <event_id.recipient_id@...> Message-ID.
    6. Transactionally mark receipt 'sent' and outbox row 'delivered'.
    """
    sm = sessionmaker or get_sessionmaker()
    cfg = settings or get_settings()

    db_now = now or datetime.now(timezone.utc)
    if db_now.tzinfo is None:
        db_now = db_now.replace(tzinfo=timezone.utc)

    recipient_id_str = lease.payload.get("recipient_id")
    if not recipient_id_str:
        async with sm() as session:
            await acknowledge(session, lease=lease, now=db_now)
        return

    recipient_id = uuid.UUID(recipient_id_str)
    booking_id = uuid.UUID(lease.payload["booking_id"])
    resource_id = uuid.UUID(lease.payload["resource_id"])

    # 1. Short transaction: get/create receipt, check skip criteria, load metadata
    async with sm() as session:
        async with session.begin():
            # Concurrency-safe atomic get-or-create using ON CONFLICT DO NOTHING
            stmt_ins = (
                pg_insert(NotificationDelivery)
                .values(
                    id=uuid.uuid4(),
                    event_id=lease.id,
                    recipient_id=recipient_id,
                    channel="email",
                    state="pending",
                )
                .on_conflict_do_nothing(index_elements=["event_id", "recipient_id", "channel"])
            )
            await session.execute(stmt_ins)

            stmt = (
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.event_id == lease.id,
                    NotificationDelivery.recipient_id == recipient_id,
                    NotificationDelivery.channel == "email",
                )
                .with_for_update()
            )
            receipt = (await session.execute(stmt)).scalar_one()

            if receipt.state in ("sent", "skipped"):
                await acknowledge(session, lease=lease, now=db_now)
                return

            # Check recipient enabled status
            user = await session.get(User, recipient_id)
            if user is None or not user.enabled:
                logger.info(
                    "Recipient %s disabled/missing; skipping %s",
                    recipient_id,
                    lease.id,
                )
                cast(Any, receipt).state = "skipped"
                await acknowledge(session, lease=lease, now=db_now)
                return

            # Check waitlist_offered expiration / status
            if lease.event_type == "waitlist_offered":
                booking = await session.get(Booking, booking_id)
                if (
                    booking is None
                    or booking.status != "offered"
                    or (booking.expires_at is not None and booking.expires_at <= db_now)
                ):
                    logger.info(
                        "waitlist_offered booking %s expired/unoffered; skipping %s",
                        booking_id,
                        lease.id,
                    )
                    cast(Any, receipt).state = "skipped"
                    await acknowledge(session, lease=lease, now=db_now)
                    return

            resource = await session.get(Resource, resource_id)
            resource_name = str(resource.name) if resource else "Resource"
            user_email = str(user.email)

    # DB connection is now closed. Send outside transaction.
    starts_at = datetime.fromisoformat(lease.payload["starts_at"])
    ends_at = datetime.fromisoformat(lease.payload["ends_at"])
    app_origin = cfg.APP_ORIGIN

    if lease.event_type == "booking_confirmed":
        subject, body = render_booking_confirmed(
            resource_name=resource_name, starts_at=starts_at, ends_at=ends_at, app_origin=app_origin
        )
    elif lease.event_type == "booking_cancelled":
        subject, body = render_booking_cancelled(
            resource_name=resource_name, starts_at=starts_at, ends_at=ends_at, app_origin=app_origin
        )
    elif lease.event_type == "waitlist_offered":
        expires_at_str = lease.payload.get("expires_at")
        expires_at = datetime.fromisoformat(expires_at_str) if expires_at_str else ends_at
        subject, body = render_waitlist_offered(
            resource_name=resource_name,
            starts_at=starts_at,
            ends_at=ends_at,
            expires_at=expires_at,
            app_origin=app_origin,
        )
    elif lease.event_type == "hold_expired":
        subject, body = render_hold_expired(
            resource_name=resource_name, starts_at=starts_at, ends_at=ends_at, app_origin=app_origin
        )
    else:
        # Unknown event type: mark skipped
        async with sm() as session:
            async with session.begin():
                await session.execute(
                    update(NotificationDelivery)
                    .where(
                        NotificationDelivery.event_id == lease.id,
                        NotificationDelivery.recipient_id == recipient_id,
                        NotificationDelivery.channel == "email",
                    )
                    .values(state="skipped")
                )
                await acknowledge(session, lease=lease, now=db_now)
        return

    stable_message_id = f"<{lease.id}.{recipient_id}@commonsbook.invalid>"
    message = EmailMessage(
        message_id=stable_message_id,
        to=user_email,
        subject=subject,
        text_body=body,
    )

    try:
        provider_message_id = await adapter.send(message)
    except EmailDeliveryError as exc:
        logger.warning("Email adapter delivery failure for lease %s: %s", lease.id, exc.category)
        async with sm() as session:
            await fail_lease(session, lease=lease, error_category=exc.category, now=db_now)
        return
    except Exception as exc:
        logger.warning("Unexpected error delivering notification for lease %s: %s", lease.id, exc)
        async with sm() as session:
            await fail_lease(session, lease=lease, error_category="transport", now=db_now)
        return

    # Transactionally mark receipt 'sent' and outbox row 'delivered'
    async with sm() as session:
        async with session.begin():
            await session.execute(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.event_id == lease.id,
                    NotificationDelivery.recipient_id == recipient_id,
                    NotificationDelivery.channel == "email",
                )
                .values(
                    state="sent",
                    provider_message_id=provider_message_id,
                    sent_at=db_now,
                )
            )
            await acknowledge(session, lease=lease, now=db_now)
