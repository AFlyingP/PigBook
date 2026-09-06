CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE users (
 id uuid PRIMARY KEY, email text NOT NULL UNIQUE,
 password_hash text NOT NULL, display_name text NOT NULL,
 role text NOT NULL DEFAULT 'member' CHECK (role IN ('member','admin')),
 enabled boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(email=lower(btrim(email)) AND length(email)<=254),
 CHECK(length(display_name) BETWEEN 1 AND 80)
);
CREATE TABLE invitations (
 id uuid PRIMARY KEY, email text NOT NULL, token_hash char(64) NOT NULL UNIQUE,
 role text NOT NULL CHECK(role IN ('member','admin')),
 created_by uuid NOT NULL REFERENCES users(id), created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL, consumed_at timestamptz,
 CHECK(email=lower(btrim(email)) AND length(email)<=254), CHECK(expires_at>created_at)
);
CREATE INDEX invitations_email_idx ON invitations(email);
CREATE TABLE refresh_tokens (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id), token_hash char(64) NOT NULL UNIQUE,
 family_id uuid NOT NULL, parent_id uuid REFERENCES refresh_tokens(id),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 family_expires_at timestamptz NOT NULL, used_at timestamptz, revoked_at timestamptz,
 CHECK(expires_at>created_at AND expires_at<=family_expires_at)
);
CREATE UNIQUE INDEX refresh_one_child_idx ON refresh_tokens(parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX refresh_family_idx ON refresh_tokens(family_id);
CREATE INDEX refresh_user_idx ON refresh_tokens(user_id);

CREATE TABLE resources (
 id uuid PRIMARY KEY, name text NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
 description text NOT NULL DEFAULT '' CHECK(length(description)<=2000),
 location text NOT NULL CHECK(length(location) BETWEEN 1 AND 200),
 active boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX resources_active_name_idx ON resources(active,name,id);

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
);
CREATE INDEX bookings_owner_idx ON bookings(user_id,created_at DESC,id);
CREATE INDEX bookings_resource_start_idx ON bookings(resource_id,lower(time_range),id);
CREATE INDEX bookings_expiry_idx ON bookings(expires_at,id) WHERE status='offered';
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
END $$;
CREATE TRIGGER bookings_update_guard BEFORE UPDATE ON bookings
 FOR EACH ROW EXECUTE FUNCTION guard_booking_update();

CREATE TABLE waitlist_entries (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
 resource_id uuid NOT NULL REFERENCES resources(id), time_range tstzrange NOT NULL,
 status text NOT NULL DEFAULT 'waiting' CHECK(status IN ('waiting','offered','accepted','cancelled','expired')),
 offered_booking_id uuid UNIQUE REFERENCES bookings(id), version integer NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(NOT isempty(time_range) AND NOT lower_inf(time_range) AND NOT upper_inf(time_range)
       AND lower_inc(time_range) AND NOT upper_inc(time_range)
       AND isfinite(lower(time_range)) AND isfinite(upper(time_range))),
 CHECK(status NOT IN ('offered','accepted') OR offered_booking_id IS NOT NULL)
);
CREATE UNIQUE INDEX waitlist_active_unique_idx ON waitlist_entries(user_id,resource_id,time_range)
 WHERE status IN ('waiting','offered');
CREATE INDEX waitlist_fifo_idx ON waitlist_entries(resource_id,created_at,id) WHERE status='waiting';
CREATE INDEX waitlist_owner_idx ON waitlist_entries(user_id,created_at DESC,id);

CREATE TABLE idempotency_keys (
 user_id uuid NOT NULL REFERENCES users(id), key uuid NOT NULL,
 request_hash char(64) NOT NULL, response_status smallint, response_body jsonb, response_headers jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 PRIMARY KEY(user_id,key), CHECK(expires_at=created_at+interval '24 hours'),
 CHECK((response_status IS NULL AND response_body IS NULL AND response_headers IS NULL) OR
       (response_status BETWEEN 200 AND 499 AND response_body IS NOT NULL AND response_headers IS NOT NULL))
);
CREATE INDEX idempotency_expiry_idx ON idempotency_keys(expires_at);

CREATE TABLE outbox (
 id uuid PRIMARY KEY, event_type text NOT NULL CHECK(event_type IN
 ('booking_confirmed','booking_cancelled','waitlist_offered','hold_expired')),
 aggregate_id uuid NOT NULL REFERENCES bookings(id), aggregate_version integer NOT NULL,
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','delivered','dead')),
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 occurred_at timestamptz NOT NULL DEFAULT now(), available_at timestamptz NOT NULL DEFAULT now(),
 lease_until timestamptz, lease_token uuid, delivered_at timestamptz, last_error text,
 UNIQUE(aggregate_id,aggregate_version,event_type),
 CHECK((status='processing' AND lease_until IS NOT NULL AND lease_token IS NOT NULL) OR
       (status<>'processing' AND lease_until IS NULL AND lease_token IS NULL))
);
CREATE INDEX outbox_pending_idx ON outbox(available_at,occurred_at,id) WHERE status='pending';
CREATE INDEX outbox_lease_idx ON outbox(lease_until,id) WHERE status='processing';
CREATE TABLE notification_deliveries (
 id uuid PRIMARY KEY, event_id uuid NOT NULL REFERENCES outbox(id),
 recipient_id uuid NOT NULL REFERENCES users(id), channel text NOT NULL CHECK(channel='email'),
 state text NOT NULL CHECK(state IN ('pending','sent','skipped')),
 provider_message_id text, sent_at timestamptz,
 UNIQUE(event_id,recipient_id,channel)
);
CREATE TABLE audit_log (
 id uuid PRIMARY KEY, actor_id uuid REFERENCES users(id), action text NOT NULL,
 target_type text NOT NULL, target_id uuid, request_id uuid NOT NULL,
 details jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_created_idx ON audit_log(created_at DESC,id);
CREATE INDEX audit_target_idx ON audit_log(target_type,target_id,created_at);
CREATE TABLE feedback (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id), rating smallint NOT NULL CHECK(rating BETWEEN 1 AND 5),
 task_completed boolean NOT NULL, difficulty text NOT NULL CHECK(length(difficulty)<=2000),
 improvement text NOT NULL CHECK(length(improvement)<=2000), consent_version text NOT NULL CHECK(consent_version='2026-09-v1'),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX feedback_user_idx ON feedback(user_id);
CREATE TABLE rate_limits (
 scope text NOT NULL, identity_hash char(64) NOT NULL, window_start timestamptz NOT NULL,
 count integer NOT NULL CHECK(count>0), PRIMARY KEY(scope,identity_hash,window_start)
);
CREATE TABLE worker_heartbeat (
 name text PRIMARY KEY CHECK(name='primary'), seen_at timestamptz NOT NULL,
 expiry_scan_at timestamptz NOT NULL
);
