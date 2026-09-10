# Administrative Inventory, Blackout, and Booking Management

This document describes the design, concurrency controls, consistency invariants, and API contracts for administrative resource management, constraint-backed blackout scheduling, and system-wide booking operations in CommonsBook.

---

## 1. Overview and Authorization Model

Administrative operations in CommonsBook provide inventory control, blackout scheduling, and booking oversight. All administrative endpoints reside under the `/api/v1/admin/*` path prefix and require the centralized `Policy.admin` authorization dependency.

### Key Architectural Invariants

- **Fail-Closed Authorization**: Authorization decisions are evaluated exclusively in `backend/app/auth/dependencies.py`. Tokens presented by callers are decoded to determine identity; the user's enabled status and role are re-read from PostgreSQL on every request. Token claims indicating `admin` role are not trusted if the database reflects `member`.
- **AuthorizedScope Propagation**: Routes receive an immutable, fail-closed `AuthorizedScope` dataclass carrying principal identity, policy, path/resource identifiers, expected versions pinned by preconditions, and query predicates. Domain services never branch on user roles or inspect JWT claims.
- **Route Domain Separation**: Member routes (`/api/v1/bookings/*`, `/api/v1/waitlist/*`) enforce own-object semantics for all users, including administrators. An administrator accessing another user's reservation through `/api/v1/bookings/{id}` receives a 404 `NOT_FOUND`. System-wide booking access is restricted to dedicated administrative routes (`/api/v1/admin/bookings/*`).
- **PostgreSQL GiST Inventory Invariant**: The non-deferrable `bookings_no_overlap` exclusion constraint remains the sole guarantee of inventory exclusivity across reservations, holds, and blackouts. No application-level availability queries or advisory locks replace this database constraint.
- **Lock Ordering**: All mutations adhere to strict global lock acquisition ordering:
  1. Acting user row locked `FOR SHARE` during in-transaction policy revalidation.
  2. Idempotency row locked `FOR UPDATE` (when applicable).
  3. Resource row(s) locked in ascending UUID order (`FOR SHARE` for creations; `FOR UPDATE` for updates, archival, and cancellations).
  4. Booking / waitlist row(s) locked `FOR UPDATE`.
  5. Transactional outbox and audit log insertions.
- **Database Clock Authority**: All time-dependent business decisions (e.g. deadline checks, start horizon validation, start/end boundaries) sample `clock_timestamp()` from PostgreSQL after acquiring row locks, never relying on application server wall clocks.

---

## 2. Endpoint Reference

| ID | Method & Path | Authorization Policy | Request Headers / Body | Success Status & Body | Specific Error Conditions | Idempotency & Preconditions |
|---|---|---|---|---|---|---|
| **E17** | `GET /api/v1/admin/resources` | `Policy.admin` | None (Query: `limit`, `offset`, `active`) | `200 Page<Resource>` | 401 `AUTH_REQUIRED` / `INVALID_TOKEN`; 403 `FORBIDDEN`; 422 validation | Standard pagination |
| **E18** | `POST /api/v1/admin/resources` | `Policy.admin` | `ResourceCreate {name, description, location}` | `201 Resource` | 401 `AUTH_REQUIRED`; 403 `FORBIDDEN`; 422 `VALIDATION_ERROR` | Headers: `Location`, `ETag` |
| **E19** | `PATCH /api/v1/admin/resources/{id}` | `Policy.admin` | Header: `If-Match`; Body: `ResourcePatch` | `200 Resource` | 404 `NOT_FOUND`; 409 `RESOURCE_IN_USE`; 412 `VERSION_MISMATCH`; 422; 428 | Precondition: `If-Match: "<version>"` |
| **E20** | `DELETE /api/v1/admin/resources/{id}` | `Policy.admin` | Header: `If-Match` | `200 Resource (active=false)` | 404 `NOT_FOUND`; 409 `RESOURCE_IN_USE`; 412 `VERSION_MISMATCH`; 428 | Precondition: `If-Match: "<version>"` |
| **E21** | `GET /api/v1/admin/resources/{id}/blackouts` | `Policy.admin` | None (Query: `limit`, `offset`) | `200 Page<Booking>` | 404 `NOT_FOUND`; 401; 403; 422 | Blackouts only |
| **E22** | `POST /api/v1/admin/resources/{id}/blackouts` | `Policy.admin` | Header: `Idempotency-Key`; Body: `BlackoutCreate` | `201 Booking` | 404 `NOT_FOUND`; 409 `SLOT_CONFLICT`, `RESOURCE_INACTIVE`; 422 | Header: `Idempotency-Key: <uuid-v4>` |
| **E23** | `DELETE /api/v1/admin/blackouts/{id}` | `Policy.admin` | Header: `If-Match` | `200 Booking (status=cancelled)` | 404 `NOT_FOUND`; 409 `TOO_LATE`, `INVALID_STATE`; 412 `VERSION_MISMATCH`; 428 | Precondition: `If-Match: "<version>"` |
| **E24** | `GET /api/v1/admin/bookings` | `Policy.admin` | None (Query: filters, `starts_at`, `ends_at`) | `200 Page<Booking>` | 401; 403; 422 `INVALID_WINDOW` / validation | Reservations only |
| **E25** | `GET /api/v1/admin/bookings/{id}` | `Policy.admin` | None | `200 Booking` | 404 `NOT_FOUND` (absent or blackout); 401; 403 | Header: `ETag: "<version>"` |
| **E26** | `POST /api/v1/admin/bookings/{id}/cancel` | `Policy.admin` | Header: `If-Match`; Body: `Cancel {reason}` | `200 Booking` | 404 `NOT_FOUND`; 409 `TOO_LATE`, `INVALID_STATE`; 412 `VERSION_MISMATCH`; 428 | Precondition: `If-Match: "<version>"` |
| **E27** | `GET /api/v1/admin/users` | `Policy.admin` | None (Query: `limit`, `offset`, `enabled`) | `200 Page<User>` | 401 `AUTH_REQUIRED` / `INVALID_TOKEN`; 403 `FORBIDDEN`; 422 validation | Standard pagination |
| **E28** | `PATCH /api/v1/admin/users/{id}` | `Policy.admin` | Header: `If-Match`; Body: `UserPatch` | `200 User` | 401; 403; 404; 409 `LAST_ADMIN`; 412 `VERSION_MISMATCH`; 422; 428 | Precondition: `If-Match: "<version>"`, Header: `ETag` |
| **E30** | `GET /api/v1/admin/audit` | `Policy.admin` | None (Query: `limit`, `offset`, `target_id`) | `200 Page<Audit>` | 401; 403; 422 | Redacted details |
| **E31** | `GET /api/v1/admin/outbox` | `Policy.admin` | None (Query: `limit`, `offset`, `status`) | `200 Page<OutboxView>` | 401; 403; 422 | Redacted error category |
| **E32** | `POST /api/v1/admin/outbox/{id}/retry` | `Policy.admin` | Body: `EmptyBody {}` | `200 OutboxView` | 401; 403; 404; 409 `INVALID_STATE` | Retries dead event atomically |
| **E34** | `GET /api/v1/admin/feedback` | `Policy.admin` | None (Query: `limit`, `offset`) | `200 Page<Feedback>` | 401; 403; 422 | Feedback restricted to admin |

---

## 3. Resource Management (E17–E20)

### Catalog Listing (E17)

`GET /api/v1/admin/resources` returns all catalog resources, including archived/inactive resources.
- **Ordering**: Deterministic order by `name` ascending, then `id` ascending.
- **Filtering**: Optional query parameter `active=true` or `active=false`. If omitted, both active and inactive resources are returned.
- **Pagination**: Standard `limit` (1..100, default 25) and `offset` (0..10000, default 0).

### Resource Creation (E18)

`POST /api/v1/admin/resources` creates a new bookable resource.
- **Payload**: `ResourceCreate {name: str(1..100), description: str(0..2000, default ""), location: str(1..200)}`.
- **Defaults**: New resources are initialized with `active=True` and `version=1`.
- **Headers**: Emits `Location: /api/v1/admin/resources/{id}` and `ETag: "1"`.
- **Audit**: Writes an audit log record with action `admin.resource_create` recording bounded metadata only (`{"fields": ["description", "location", "name"]}`) without freeform text values.

### Guarded Resource Patch (E19)

`PATCH /api/v1/admin/resources/{id}` allows partial updates to resource metadata.
- **Precondition**: Requires an `If-Match` header containing a single strong entity tag matching the current stored version (e.g. `If-Match: "1"`). Stale version mismatch produces `412 VERSION_MISMATCH` before state evaluations.
- **Payload**: `ResourcePatch` permits updating `name`, `description`, `location`, and `active`. At least one field must be provided; explicit `null` values are rejected with 422. Extra fields are rejected.
- **Locking & Concurrency**: Acquires `Resource FOR UPDATE`. Two concurrent same-version patch requests serialize: the first succeeds with `200 OK` and increments version; the second detects the version advancement and returns `412 VERSION_MISMATCH`.
- **Deactivation Protection**: Patching `active=False` checks for active inventory usage. If any confirmed or offered booking has `upper(time_range) > now` or any waiting waitlist entry exists, the update is rejected with `409 RESOURCE_IN_USE`, preserving the resource's active state.

### Resource Archival (E20)

`DELETE /api/v1/admin/resources/{id}` soft-archives a resource by setting `active=False`.
- **Precondition**: Requires `If-Match: "<version>"`. Missing header returns 428; stale version returns 412.
- **State-Preserving In-Use Rejection**: Archival is rejected with `409 RESOURCE_IN_USE` if:
  1. Any confirmed reservation or blackout has `upper(time_range) > clock_timestamp()`.
  2. Any offered reservation hold has `upper(time_range) > clock_timestamp()`.
  3. Any waitlist entry exists in `waiting` status for this resource.
  Past completed bookings (`upper(time_range) <= now`) do not block archival. When rejected, no resource fields are modified and no audit log is generated.
- **Archive-vs-Create Race Serialization**: Booking creation acquires `Resource FOR SHARE`; resource archival acquires `Resource FOR UPDATE`. If creation commits first, archival observes the newly committed active booking and returns `409 RESOURCE_IN_USE`. If archival commits first, creation reads `active=False` and returns `409 RESOURCE_INACTIVE`. Both interleavings yield legal serial outcomes.
- **Audit**: Successful archival emits `ETag: "<new_version>"` and records `admin.resource_archive` with `{"fields": ["active"], "new_active": false, "old_active": true}`.

---

## 4. Constraint-Backed Blackouts (E21–E23)

### Blackout Domain Model

Blackouts represent administrator-mandated maintenance or facility closures.
- **Unified Inventory Table**: Stored directly in the `bookings` table with `kind='blackout'`, `user_id=NULL`, and `created_by` set to the acting administrator.
- **Exclusion Invariant**: Covered by the same GiST exclusion constraint `bookings_no_overlap`. A blackout cannot overlap confirmed reservations, offered holds, or other blackouts on the same resource.
- **Window Rules**:
  - Timestamps must include explicit UTC offsets on 30-minute boundaries (zero seconds/microseconds).
  - Duration: minimum 30 minutes, maximum 30 days.
  - End time must be in the future (`ends_at > clock_timestamp()`).
  - Start time may start immediately (at or after current 30-minute slot).

### Idempotent Blackout Creation (E22)

`POST /api/v1/admin/resources/{id}/blackouts` schedules a blackout.
- **Idempotency Key**: Requires a UUID v4 `Idempotency-Key` header. The key namespace is shared across all inventory-creating endpoints.
- **Request Hashing**: Computes SHA-256 of canonical JSON `{"body": ..., "method": "POST", "path": "/api/v1/admin/resources/{id}/blackouts"}`. Replay with identical payload within 24 hours returns the stored response with `Idempotency-Replayed: true`. Payload or method/path mismatch returns `422 IDEMPOTENCY_KEY_MISMATCH` without replacing stored state.
- **Conflict Handling**: Overlap with an existing reservation or blackout raises PostgreSQL exclusion violation `23P01`, which is caught inside a savepoint and cached as a stored `409 SLOT_CONFLICT` response.
- **Inactive / Missing Resource**: Inactive resource produces a cached `409 RESOURCE_INACTIVE` response; nonexistent resource returns an uncached `404 NOT_FOUND` and rolls back the transaction.
- **No Notification Event**: Blackout creation emits zero outbox events; members are not emailed about blackout creation.
- **Audit**: Writes `admin.blackout_create` with resource ID and ISO8601 interval boundaries.

### Blackout Listing (E21)

`GET /api/v1/admin/resources/{id}/blackouts` lists blackout intervals for a given resource.
- Scoped strictly to `kind='blackout'`; normal reservations are never exposed on this route.
- Ordered by `created_at` descending, then `id` ascending.
- Nonexistent resource returns `404 NOT_FOUND`.

### Blackout Cancellation and Waitlist Promotion (E23)

`DELETE /api/v1/admin/blackouts/{id}` cancels an active blackout.
- **Lock Ordering**: Locks `Resource FOR UPDATE`, then the blackout `Booking FOR UPDATE`.
- **Preconditions**: Requires `If-Match: "<version>"`. Stale version returns 412 before state evaluations.
- **State Transition**: Sets `status='cancelled'` and increments `version`. If already cancelled at the current version, returns `200 OK` idempotently without secondary side effects.
- **Time Invariant**: Completed blackouts (`upper(time_range) <= clock_timestamp()`) cannot be cancelled; attempts return `409 TOO_LATE` ("A completed blackout cannot be cancelled").
- **Atomic Waitlist Promotion**: Releasing the blackout invokes `promote_waiters(session, resource_id, db_now)` within the same transaction. Eligible waiters whose requested windows were blocked by the blackout are immediately promoted to `offered` holds.
- **No Outbox Notification**: Blackout cancellation does not append a cancellation email event to the outbox.
- **Audit**: Writes `admin.blackout_cancel` with actor and resource identifiers.

---

## 5. Administrator Booking Operations (E24–E26)

### System-Wide Booking Queries (E24)

`GET /api/v1/admin/bookings` provides administrators with a paginated view of reservations across all users and resources.
- **Reservations Only**: Always filters `kind='reservation'`. Blackouts are excluded and only visible via E21.
- **Filter Parameters**:
  - `resource_id` (UUID): Filter by specific resource.
  - `user_id` (UUID): Filter by specific member.
  - `status` (`confirmed`, `offered`, `cancelled`, `expired`): Filter by booking lifecycle status.
  - `starts_at` and `ends_at` (pair): Range overlap query `Booking.time_range && tstzrange(starts_at, ends_at, '[)')`. Both parameters must be provided together. Span cannot exceed 90 days; non-conforming or inverted ranges return `422 INVALID_WINDOW`.
- **Ordering**: Stable pagination ordered by `created_at` descending, then `id` ascending.

### Booking Detail View (E25)

`GET /api/v1/admin/bookings/{id}` retrieves details of any reservation in the system.
- Emits current entity version in `ETag: "<version>"`.
- Accessing a blackout ID or nonexistent booking returns `404 NOT_FOUND`.

### Administrative Booking Cancellation (E26)

`POST /api/v1/admin/bookings/{id}/cancel` enables administrators to cancel reservations on behalf of members or operational needs.
- **Domain Logic Reuse**: Reuses `cancel_booking` in `backend/app/bookings/service.py` via `AuthorizedScope` rather than branching on roles in domain logic.
- **Cancellation Authority Rules**:
  - **Future Reservations**: Cancellable by both members (via E12) and administrators (via E26).
  - **Running Reservations**: Members cannot cancel ongoing bookings (`starts_at <= now`). Administrators **can** cancel currently running reservations (`starts_at <= now < ends_at`) to free capacity or resolve incidents.
  - **Completed Reservations**: Neither members nor administrators can cancel completed bookings (`ends_at <= now`). Historical occupancy is immutable; attempts return `409 TOO_LATE`.
- **Atomic Side Effects**:
  1. Sets booking `status='cancelled'`, increments `version`, clears `expires_at`, updates `updated_at`.
  2. Transitions any linked offered waitlist entry to `status='cancelled'`.
  3. Appends `booking_cancelled` event to the transactional outbox to notify the reservation owner.
  4. Appends audit log record `admin.booking_cancel` with actor ID, target booking ID, and reason length.
  5. Invokes `promote_waiters` to offer released capacity to the next eligible waiter in the queue.
  All five mutations commit together in a single transaction.



---

## 6. Administrator User Lifecycle Management (E27–E28)

### User Directory Listing (E27)

`GET /api/v1/admin/users` lists user accounts across the system.
- **Ordering**: Deterministic order by `created_at` descending, then `id` ascending.
- **Filtering**: Optional query parameter `enabled=true` or `enabled=false`.
- **Pagination**: Standard pagination with `limit` (1..100, default 25) and `offset` (0..10000, default 0).

### Guarded User Patch and Session Revocation (E28)

`PATCH /api/v1/admin/users/{id}` modifies user account role or active status.
- **Precondition**: Requires an `If-Match` header pinning the expected user version. Missing header returns 428 `PRECONDITION_REQUIRED`; malformed format returns 422 `VALIDATION_ERROR`; stale version returns 412 `VERSION_MISMATCH` strictly evaluated before state checks.
- **Payload**: `UserPatch` permits updating `role` (`member` or `admin`) and `enabled` (`true` or `false`). At least one field must be supplied; explicit null values or extra properties are rejected with 422.
- **Concurrency & Serialization**: Mutating user requests take transactional advisory lock `714001` first in the authorization dependency before acquiring any user row lock. Actor and target users are then locked in strict UUID order.
- **Last Active Administrator Protection**: Attempting to demote or disable the last active administrator returns 409 `LAST_ADMIN`. Concurrent demotions serialize on advisory lock 714001, ensuring that one succeeds and the second fails closed.
- **Session Revocation on Disable**: Disabling an account (`enabled=False`) immediately revokes all active refresh tokens and token families for that user within the same database transaction. The user's historical reservations, blackouts, and waitlist entries are preserved intact.
- **Stale Privilege Invalidation**: User role and enabled status are reloaded from PostgreSQL on every request. Tokens minted prior to demotion or disabling immediately lose privilege.
- **Audit Logging**: Successful updates record `admin.user_update` with details restricted to changed field names and safe old/new role and enabled values.

---

## 7. Operations: Audit and Dead-Letter Operations (E30–E32)

### Audit Trail Inspection (E30)

`GET /api/v1/admin/audit` retrieves immutable system audit logs.
- **Ordering**: Deterministic order by `created_at` descending, then `id` ascending.
- **Filtering**: Optional query parameter `target_id` (UUID).
- **Details Redaction**: Audit `details` are sanitized using a strict allowlist. Raw passwords, authentication tokens, cookies, invitation links, or un-redacted user content are never exposed.
- **Immutability Invariant**: The runtime database user holds `SELECT` and `INSERT` privileges only on `audit_log`; update and deletion operations are rejected by database permissions.

### Outbox and Dead-Letter Monitoring (E31)

`GET /api/v1/admin/outbox` exposes notification background delivery status.
- **Ordering**: Deterministic order by `occurred_at` descending, then `id` ascending.
- **Filtering**: Optional query parameter `status` (`pending`, `processing`, `delivered`, or `dead`).
- **Error Redaction**: The `last_error` field is exposed only as a sanitized error category (e.g. `connection_timeout`, `smtp_error`), preventing credential or message body leakage.

### Dead-Letter Retry (E32)

`POST /api/v1/admin/outbox/{id}/retry` retries a failed notification event.
- **State Invariant**: Only events currently in `dead` status may be retried. Attempting to retry an event in `pending`, `processing`, or `delivered` status returns 409 `INVALID_STATE`.
- **Atomic Transition**: Atomically resets status from `dead` to `pending`, sets `attempts = 0`, sets `available_at = clock_timestamp()`, clears `last_error`, and records an `admin.outbox_retry` audit entry within the same transaction.
- **Delivery Receipt Preservation**: The event ID and any recorded delivery receipt in `notification_deliveries` are retained, preventing already-dispatched emails from being re-sent.

---

## 8. Audit Logging and Security Controls

All administrative mutations are recorded in the append-only `audit_log` table:

| Action | Target Type | Target ID | Details Allowlist |
|---|---|---|---|
| `admin.resource_create` | `resource` | Resource UUID | `{"fields": ["description", "location", "name"]}` |
| `admin.resource_patch` | `resource` | Resource UUID | `{"fields": [...]}` plus `"old_active": bool, "new_active": bool` when active changes |
| `admin.resource_archive` | `resource` | Resource UUID | `{"fields": ["active"], "new_active": false, "old_active": bool}` |
| `admin.blackout_create` | `booking` | Blackout UUID | `resource_id`, `starts_at`, `ends_at` |
| `admin.blackout_cancel` | `booking` | Blackout UUID | `resource_id` |
| `admin.booking_cancel` | `booking` | Booking UUID | `resource_id`, `reason_length` |

- **Security & Privacy**: Payloads never record raw passwords, tokens, full member email addresses, user-entered resource text (`name`, `location`, `description`), or un-redacted private messages. Free-text cancellation reasons record only character length (`reason_length`). Resource creations and patches record only changed field names and safe boolean values.
- **Atomic Commit**: The audit entry is created using the transaction session and commits atomically with domain entity changes; failure of either rolls back both.

---

## 9. Error Envelopes and Status Codes

All errors adhere to the standard CommonsBook error envelope:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable explanation",
    "details": {}
  }
}
```

- `401 AUTH_REQUIRED`: Missing or malformed Authorization header.
- `401 INVALID_TOKEN`: Invalid JWT signature, expired token, or disabled account.
- `403 FORBIDDEN`: Caller role is `member` when accessing `Policy.admin` routes.
- `404 NOT_FOUND`: Target resource or booking does not exist, or reservation route addressed a blackout.
- `409 RESOURCE_IN_USE`: Archival or deactivation rejected because confirmed/offered bookings or waiters exist.
- `409 RESOURCE_INACTIVE`: Blackout creation rejected because the target resource is archived.
- `409 SLOT_CONFLICT`: Blackout overlaps an existing confirmed reservation, offered hold, or blackout.
- `409 TOO_LATE`: Attempted cancellation of an already completed reservation or blackout.
- `409 INVALID_STATE`: Object is in a state that cannot be transitioned (e.g. retrying a non-dead outbox event).
- `409 LAST_ADMIN`: Operation rejected because it would demote or disable the last active administrator.
- `412 VERSION_MISMATCH`: Precondition version in `If-Match` does not match stored row version (evaluated before state checks).
- `422 VALIDATION_ERROR`: Schema validation failure, null patch fields, or malformed `If-Match` format.
- `422 IDEMPOTENCY_KEY_REQUIRED` / `IDEMPOTENCY_KEY_INVALID`: Missing or non-UUID-v4 idempotency key.
- `422 IDEMPOTENCY_KEY_MISMATCH`: Reuse of an idempotency key with differing method, path, or body.
- `422 INVALID_WINDOW`: Interval fails 30-minute alignment, end before start, duration out of bounds, or filter span > 90 days.
- `428 PRECONDITION_REQUIRED`: Mutating route called without required `If-Match` header.
- `503 RETRYABLE_UNAVAILABLE`: Transient database connection failure or lock acquisition timeout.

---

## 10. Current Limitations

- **Self-Service Password Recovery**: Password reset via email is excluded for this invitation-only pilot. Account recovery is performed via operator CLI tooling (`scripts/reset_password.py`).
