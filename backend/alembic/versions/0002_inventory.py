"""0002_inventory

Revision ID: 0002_inventory
Revises: 0001_identity
Create Date: 2026-09-06

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_inventory"
down_revision: Union[str, Sequence[str], None] = "0001_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.execute("""
CREATE TABLE resources (
 id uuid PRIMARY KEY, name text NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
 description text NOT NULL DEFAULT '' CHECK(length(description)<=2000),
 location text NOT NULL CHECK(length(location) BETWEEN 1 AND 200),
 active boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
)
""")

    op.execute("CREATE INDEX resources_active_name_idx ON resources(active,name,id)")

    op.execute("""
CREATE TABLE bookings (
 id uuid PRIMARY KEY, resource_id uuid NOT NULL REFERENCES resources(id),
 user_id uuid REFERENCES users(id), created_by uuid NOT NULL REFERENCES users(id),
 kind text NOT NULL DEFAULT 'reservation' CHECK(kind IN ('reservation','blackout')),
 time_range tstzrange NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','confirmed','offered','cancelled','expired')),
 expires_at timestamptz,
 cancellation_reason text CHECK(length(cancellation_reason)<=500),
 version integer NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(NOT isempty(time_range) AND NOT lower_inf(time_range) AND NOT upper_inf(time_range)
       AND lower_inc(time_range) AND NOT upper_inc(time_range)
       AND isfinite(lower(time_range)) AND isfinite(upper(time_range))),
 CHECK((kind='reservation' AND user_id IS NOT NULL) OR
       (kind='blackout' AND user_id IS NULL AND status IN ('confirmed','cancelled'))),
 CHECK((status='offered' AND expires_at IS NOT NULL AND expires_at<=lower(time_range)) OR
       (status<>'offered' AND expires_at IS NULL)),
 CONSTRAINT bookings_no_overlap EXCLUDE USING gist
 (resource_id WITH =, time_range WITH &&)
 WHERE (status IN ('confirmed','offered'))
)
""")

    op.execute("CREATE INDEX bookings_owner_idx ON bookings(user_id,created_at DESC,id)")
    op.execute(
        "CREATE INDEX bookings_resource_start_idx ON bookings(resource_id,lower(time_range),id)"
    )
    op.execute("CREATE INDEX bookings_expiry_idx ON bookings(expires_at,id) WHERE status='offered'")

    op.execute("""
CREATE FUNCTION guard_booking_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.resource_id,NEW.user_id,NEW.kind,NEW.time_range,NEW.created_by)
    IS DISTINCT FROM ROW(OLD.resource_id,OLD.user_id,OLD.kind,OLD.time_range,OLD.created_by) THEN
   RAISE EXCEPTION 'immutable booking identity/window' USING ERRCODE='23514';
 END IF;
 IF NEW.status<>OLD.status AND NOT (
   (OLD.status='pending' AND NEW.status IN ('confirmed','cancelled','expired')) OR
   (OLD.status='offered' AND NEW.status IN ('confirmed','cancelled','expired')) OR
   (OLD.status='confirmed' AND NEW.status='cancelled')) THEN
   RAISE EXCEPTION 'invalid booking transition' USING ERRCODE='23514';
 END IF;
 IF NEW.version<>OLD.version+1 THEN
   RAISE EXCEPTION 'version must increment' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$
""")

    op.execute("""
CREATE TRIGGER bookings_update_guard BEFORE UPDATE ON bookings
 FOR EACH ROW EXECUTE FUNCTION guard_booking_update()
""")

    op.execute("""
CREATE TABLE waitlist_entries (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
 resource_id uuid NOT NULL REFERENCES resources(id), time_range tstzrange NOT NULL,
 status text NOT NULL DEFAULT 'waiting'
   CHECK(status IN ('waiting','offered','accepted','cancelled','expired')),
 offered_booking_id uuid UNIQUE REFERENCES bookings(id),
 version integer NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(NOT isempty(time_range) AND NOT lower_inf(time_range) AND NOT upper_inf(time_range)
       AND lower_inc(time_range) AND NOT upper_inc(time_range)
       AND isfinite(lower(time_range)) AND isfinite(upper(time_range))),
 CHECK(status NOT IN ('offered','accepted') OR offered_booking_id IS NOT NULL)
)
""")

    op.execute("""
CREATE UNIQUE INDEX waitlist_active_unique_idx ON waitlist_entries(user_id,resource_id,time_range)
 WHERE status IN ('waiting','offered')
""")
    op.execute(
        "CREATE INDEX waitlist_fifo_idx "
        "ON waitlist_entries(resource_id,created_at,id) WHERE status='waiting'"
    )
    op.execute("CREATE INDEX waitlist_owner_idx ON waitlist_entries(user_id,created_at DESC,id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS waitlist_owner_idx")
    op.execute("DROP INDEX IF EXISTS waitlist_fifo_idx")
    op.execute("DROP INDEX IF EXISTS waitlist_active_unique_idx")
    op.execute("DROP TABLE IF EXISTS waitlist_entries")
    op.execute("DROP TRIGGER IF EXISTS bookings_update_guard ON bookings")
    op.execute("DROP FUNCTION IF EXISTS guard_booking_update()")
    op.execute("DROP INDEX IF EXISTS bookings_expiry_idx")
    op.execute("DROP INDEX IF EXISTS bookings_resource_start_idx")
    op.execute("DROP INDEX IF EXISTS bookings_owner_idx")
    op.execute("DROP TABLE IF EXISTS bookings")
    op.execute("DROP INDEX IF EXISTS resources_active_name_idx")
    op.execute("DROP TABLE IF EXISTS resources")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
