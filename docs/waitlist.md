# Waitlist Membership, Promotion, and Offer Lifecycle

This document describes the design, concurrency controls, consistency invariants, and API contracts for waitlist membership, promotion on release, and offer lifecycle actions in CommonsBook.

---

## 1. Overview and Lifecycle Model

CommonsBook provides a waitlist mechanism allowing members to register interest in specific bookable resource intervals that are currently occupied by confirmed reservations or blackouts. When occupancy is released, the service atomically promotes the oldest eligible waiter by issuing a time-bounded booking hold (`status='offered'`).

### Lifecycle State Machine

A waitlist entry (`waitlist_entries`) transitions through the following lifecycle states:

```text
                    ┌─────────────────────────┐
                    │         waiting         │
                    └────────────┬────────────┘
                                 │
           ┌─────────────────────┼─────────────────────┐
           │ (slot freed)        │ (caller withdraws)  │ (too late / disabled)
           ▼                     ▼                     ▼
┌─────────────────────┐┌───────────────────┐┌───────────────────┐
│       offered       ││     cancelled     ││      expired      │
└──────────┬──────────┘└───────────────────┘└───────────────────┘
           │
     ┌─────┴─────┐
     │ (accept)  │ (decline / deadline expiry)
     ▼           ▼
┌──────────┐┌───────────────────┐
│ accepted ││ cancelled/expired │
└──────────┘└───────────────────┘
```

- **`waiting`**: Initial state upon joining via `POST /api/v1/waitlist`. The desired time window is occupied by an active reservation or blackout.
- **`offered`**: Promoted atomically when inventory becomes free. Linked to a fresh reservation booking with `status='offered'` and a finite `expires_at` deadline.
- **`accepted`**: Terminal state reached when the waiter accepts the offer before `expires_at` via `POST /api/v1/waitlist/{id}/accept`. Transitions the linked booking to `status='confirmed'`.
- **`cancelled`**: Terminal state reached when withdrawn by its owner via `DELETE /api/v1/waitlist/{id}` or when an offered hold is declined.
- **`expired`**: Terminal state reached if the entry's start time is within 15 minutes of the current database time, if the owner is disabled, or if the offered hold deadline expires before acceptance (either during offer acceptance attempts or automatically via the 30-second background scheduler). No automatic requeue.

---

## 2. API Endpoints

| ID | Method & Path | Authorization Policy | Request Body | Success Response | Error Conditions |
|---|---|---|---|---|---|
| **E13** | `POST /api/v1/waitlist` | `authenticated` | `WaitCreate {resource_id, starts_at, ends_at}` | `201 WaitEntry` | 404 `NOT_FOUND`; 409 `SLOT_AVAILABLE`, `ALREADY_WAITLISTED`, `ALREADY_BOOKED`, `RESOURCE_INACTIVE`, `WAITLIST_FULL`; 422 `INVALID_WINDOW` |
| **E14** | `GET /api/v1/waitlist` | `own_waitlist` | None (Query: `limit`, `offset`, `status`) | `200 Page<WaitEntry>` | 401 `AUTH_REQUIRED` / `INVALID_TOKEN`; 422 validation |
| **E15** | `DELETE /api/v1/waitlist/{id}` | `own_waitlist` | Header: `If-Match: "<version>"` | `200 WaitEntry` | 404 `NOT_FOUND`; 409 `TOO_LATE`, `INVALID_STATE`; 412 `VERSION_MISMATCH`; 428 `PRECONDITION_REQUIRED` |
| **E16** | `POST /api/v1/waitlist/{id}/accept` | `own_waitlist` | Header: `If-Match: "<version>"`, Body: `AcceptOffer {}` | `200 Booking` | 404 `NOT_FOUND`; 409 `HOLD_EXPIRED`, `INVALID_STATE`; 412 `VERSION_MISMATCH`; 428 `PRECONDITION_REQUIRED` |

### Schemas

- **`WaitCreate`**: `{ resource_id: UUID, starts_at: ISO8601, ends_at: ISO8601 }`. No idempotency key required; no booking is created.
- **`WaitEntry`**: `{ id, user_id, resource_id, starts_at, ends_at, status, offered_booking_id, version, created_at, updated_at }`.
- **`AcceptOffer`**: Empty JSON object `{}` with `extra="forbid"`.

---

## 3. Concurrency, Invariants, and Lock Hierarchy

CommonsBook maintains strict PostgreSQL `READ COMMITTED` isolation and avoids deadlocks by acquiring locks in a globally deterministic order (Spec 5.1).

### Lock Ordering

For all waitlist mutations (membership join, promotion, withdrawal, and hold acceptance):
1. **User Row (`users`)**: Centralized authorization dependency acquires `SELECT ... FOR SHARE` to verify active, enabled status without blocking concurrent operations.
2. **Resource Row (`resources`)**: Acquired exclusively via `SELECT ... FOR UPDATE`. Holding the resource lock exclusively guarantees that inventory release, cancellation, and promotion commit atomically relative to fresh reservation creates.
3. **Waitlist / Booking Rows**: Reselected and locked via `SELECT ... FOR UPDATE` using owner-scoped predicates supplied by the authorization scope.
4. **Outbox / Audit Logs**: Transactional inserts into append-only tables.

A booking row is **never** locked before the resource row.

### Exclusion Invariant

The sole database inventory guarantee is the non-deferrable GiST exclusion constraint on the `bookings` table:

```sql
CONSTRAINT bookings_no_overlap EXCLUDE USING gist (
  resource_id WITH =,
  time_range WITH &&
) WHERE (status IN ('confirmed', 'offered'))
```

Application locks serialize multi-row coordination, but mutual exclusion is guaranteed by the GiST index.

---

## 4. Waitlist Admission and Fairness Boundaries

### Busy-Window Requirement and Own-Overlap Rejection
- A new waitlist entry is admitted only if an active booking (`status IN ('confirmed', 'offered')`) or blackout overlaps the requested interval. If the slot is free, the API returns 409 `SLOT_AVAILABLE`, directing callers to book directly.
- A caller who already owns an active reservation overlapping the desired interval is rejected with 409 `ALREADY_BOOKED`.
- Active waitlist membership is unique per user, resource, and exact time range (backed by the unique partial index `waitlist_active_unique_idx`). An existing active entry returns 409 `ALREADY_WAITLISTED`.

### Admission Cap (500 Active Entries per Resource)
- Active waitlist entries (`status IN ('waiting', 'offered')`) are strictly capped at **500 per resource**.
- The cap is evaluated within the domain transaction **after** taking the resource `FOR UPDATE` lock. This ensures concurrent requests cannot exceed 500 active entries through application routes.
- Duplicate membership (`ALREADY_WAITLISTED`) and own-booking conflict (`ALREADY_BOOKED`) checks take precedence over the capacity check. An existing member attempting to join a queue that is already at capacity receives 409 `ALREADY_WAITLISTED` rather than `WAITLIST_FULL`.
- Terminal entries (`accepted`, `cancelled`, `expired`) do not count toward the cap.

### FIFO Fairness Boundary
- FIFO ordering is strict per **resource and exact requested time window**, ordered by `(created_at ASC, id ASC)`.
- A blocked older entry requesting an overlapping unavailable window does **not** block younger entries requesting disjoint, available capacity.
- No partial allocations, window splitting, or preemption are supported.
- FIFO ordering applies to eligible existing waiters during atomic promotion. The service makes no fairness claims over independent direct database writers or post-deadline requests.

---

## 5. Promotion on Inventory Release (`promote_waiters`)

When confirmed capacity is released (e.g., via `POST /api/v1/bookings/{id}/cancel`, or via administrative cancellation once implemented), `promote_waiters` executes **in the same transaction before commit**:

1. **Full Keyset Scan**:
   - The service scans all waiting entries for the resource ordered by `(created_at ASC, id ASC)`.
   - Results are paged in batches of 100 using a keyset cursor `(created_at, id) > (last_created_at, last_id)` within the transaction.
   - The page size of 100 is an internal fetch size, **not a total scan cap**. The scan continues until all waiting entries for the resource have been evaluated.
   - No in-memory cursor is persisted across transactions.

2. **Horizon and User Eligibility**:
   - Sample the database clock `clock_timestamp()`.
   - Entries whose requested start time is less than `db_time + 15 minutes` are transitioned to `expired` (`version = version + 1`).
   - Entries whose owners have been disabled are transitioned to `expired` (`version = version + 1`).

3. **Offer Creation and Savepoint Isolation**:
   - For each remaining eligible entry whose entire window is free of active bookings (`confirmed` or `offered`), the service samples `clock_timestamp()` immediately prior to insertion.
   - The hold expiration is calculated as:
     ```python
     expires_at = min(db_now + timedelta(minutes=15), entry.time_range.lower)
     ```
     This guarantees compliance with the database constraint `expires_at <= lower(time_range)`.
   - A fresh reservation booking is inserted with `status='offered'`, owner set to the entry's user, and linked to the waitlist entry via `offered_booking_id`.
   - The insertion executes inside a nested transaction (savepoint). If an independent database writer creates an overlapping booking, the resulting `23P01` constraint violation rolls back only that savepoint; the entry remains `waiting` and the scan continues for subsequent disjoint windows.
   - On successful offer creation, the entry transitions to `offered` (`version = version + 1`) and a `waitlist_offered` outbox event is appended atomically.

---

## 6. Offer Acceptance and Withdrawal

### Offer Acceptance (`POST /api/v1/waitlist/{id}/accept`)
- Validates the required `If-Match` header. A stale version immediately returns 412 `VERSION_MISMATCH` before evaluating state.
- Reselects the entry and linked booking under resource and row locks (`FOR UPDATE`).
- Samples the database clock `clock_timestamp()`. If `db_now >= booking.expires_at`, **the deadline has expired**:
  - The service persists the entry and booking transitions to `status='expired'` (`version = version + 1`).
  - Appends a `hold_expired` transactional outbox event.
  - Promotes subsequent eligible waiters on the released slot.
  - Returns a committed 409 `HOLD_EXPIRED` response.
- If accepted before deadline (`db_now < booking.expires_at`):
  - Transitions the linked booking to `confirmed`, clears `expires_at`, increments its version.
  - Transitions the waitlist entry to `accepted`, increments its version.
  - Appends a `booking_confirmed` outbox event.
  - Returns 200 with the confirmed `Booking` representation and `ETag: "<version>"`.

### Entry Withdrawal and Decline (`DELETE /api/v1/waitlist/{id}`)
- Requires `If-Match` pinning the expected entry version.
- If the entry is in `waiting` status:
  - Transitions the entry to `cancelled` (`version = version + 1`). No booking is created, no outbox event is appended, and no promotion is triggered.
- If the entry is in `offered` status:
  - Transitions both the waitlist entry and its linked offered booking to `cancelled` (`version = version + 1`, `expires_at = None`).
  - Atomically invokes `promote_waiters` to offer the released slot to the next eligible waiter.
  - A voluntary decline of an offered hold by the waiter emits no notification outbox event, as the withdrawal originates directly from the recipient.
- If the slot has already started (`db_now >= entry.starts_at`), returns 409 `TOO_LATE`.

### Background Hold Expiry and Promotion Scheduler
In addition to lazy expiration during user acceptance attempts, the background worker daemon periodically discovers resources with overdue holds (`status='offered' AND expires_at <= clock_timestamp()`) or waiting entries every 30 seconds. For each candidate resource, the scheduler acquires the resource lock (`FOR UPDATE SKIP LOCKED`), samples `clock_timestamp()` after lock acquisition, transitions overdue bookings and linked waitlist entries to `status='expired'`, appends `hold_expired` outbox events, and invokes `promote_waiters` with the standard 15-minute hold horizon to advance subsequent waiters automatically.

---

## 7. Known Limitations and Verification Notes

1. **Document Checks Execution**: The verification script (`scripts/verify.py`) does not implement an automated document-check executor; `document_checks` in verification fragments remains empty to avoid runner failures, and documentation files are maintained directly as repository artifacts.
2. **Uncoordinated Database Writers**: FIFO fairness guarantees apply to operations routed through application transactions holding the resource lock. Independent direct SQL writers without resource locking can trigger savepoint rollback during offer creation, which leaves the entry in `waiting` status without starving subsequent disjoint requests.
