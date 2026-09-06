"""0003_delivery

Revision ID: 0003_delivery
Revises: 0002_inventory
Create Date: 2026-09-06

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_delivery"
down_revision: Union[str, Sequence[str], None] = "0002_inventory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE idempotency_keys (
 user_id uuid NOT NULL REFERENCES users(id), key uuid NOT NULL,
 request_hash char(64) NOT NULL, response_status smallint,
 response_body jsonb, response_headers jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 PRIMARY KEY(user_id,key), CHECK(expires_at=created_at+interval '24 hours'),
 CHECK((response_status IS NULL AND response_body IS NULL AND response_headers IS NULL) OR
       (response_status BETWEEN 200 AND 499 AND response_body IS NOT NULL
        AND response_headers IS NOT NULL))
)
""")

    op.execute("CREATE INDEX idempotency_expiry_idx ON idempotency_keys(expires_at)")

    op.execute("""
CREATE TABLE outbox (
 id uuid PRIMARY KEY, event_type text NOT NULL CHECK(event_type IN
 ('booking_confirmed','booking_cancelled','waitlist_offered','hold_expired')),
 aggregate_id uuid NOT NULL REFERENCES bookings(id), aggregate_version integer NOT NULL,
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
 status text NOT NULL DEFAULT 'pending'
   CHECK(status IN ('pending','processing','delivered','dead')),
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 occurred_at timestamptz NOT NULL DEFAULT now(), available_at timestamptz NOT NULL DEFAULT now(),
 lease_until timestamptz, lease_token uuid, delivered_at timestamptz, last_error text,
 UNIQUE(aggregate_id,aggregate_version,event_type),
 CHECK((status='processing' AND lease_until IS NOT NULL AND lease_token IS NOT NULL) OR
       (status<>'processing' AND lease_until IS NULL AND lease_token IS NULL))
)
""")

    op.execute(
        "CREATE INDEX outbox_pending_idx "
        "ON outbox(available_at,occurred_at,id) WHERE status='pending'"
    )
    op.execute("CREATE INDEX outbox_lease_idx ON outbox(lease_until,id) WHERE status='processing'")

    op.execute("""
CREATE TABLE notification_deliveries (
 id uuid PRIMARY KEY, event_id uuid NOT NULL REFERENCES outbox(id),
 recipient_id uuid NOT NULL REFERENCES users(id), channel text NOT NULL CHECK(channel='email'),
 state text NOT NULL CHECK(state IN ('pending','sent','skipped')),
 provider_message_id text, sent_at timestamptz,
 UNIQUE(event_id,recipient_id,channel)
)
""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notification_deliveries")
    op.execute("DROP INDEX IF EXISTS outbox_lease_idx")
    op.execute("DROP INDEX IF EXISTS outbox_pending_idx")
    op.execute("DROP TABLE IF EXISTS outbox")
    op.execute("DROP INDEX IF EXISTS idempotency_expiry_idx")
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
