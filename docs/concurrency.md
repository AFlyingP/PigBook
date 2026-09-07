# Concurrency and Consistency Architecture

This document describes the concurrency model, database invariants, locking strategies, transaction boundaries, and event persistence mechanisms governing the booking primitive.

---

## 1. The Database as the Sole Inventory Invariant

Inventory correctness relies exclusively on PostgreSQL's immediate, non-deferrable GiST exclusion constraint on the `bookings` table (`bookings_no_overlap`):

```sql
CONSTRAINT bookings_no_overlap EXCLUDE USING gist (
  resource_id WITH =,
  time_range WITH &&
) WHERE (status IN ('confirmed', 'offered'))
```

### Architectural Principles

1. **No Application-Level Preflight Overlap Checks**:
   Application-level `SELECT` queries checking for slot availability before insertion are subject to race conditions and phantom reads under `READ COMMITTED` isolation. Two concurrent transactions can both evaluate availability to true and attempt conflicting inserts. Therefore, the booking primitive does not run an availability preflight before insertion. Instead, it attempts the insertion directly and relies on the database exclusion constraint to guarantee mutual exclusion.

2. **No Advisory Locks or Coarse Serialization**:
   Advisory locks or table-level locks artificially serialize independent operations, degrade throughput, and introduce failure modes if lock acquisition is interrupted. The GiST exclusion index operates at row/range granularity, allowing disjoint requests to execute in parallel without contention.

3. **Status Filtering**:
   The exclusion constraint index applies only to rows with `status IN ('confirmed', 'offered')`. Rows with `status` in `('pending', 'cancelled', 'expired')` do not occupy inventory. When a booking is cancelled or expires, the slot immediately becomes available to subsequent inserts without requiring row deletion or schema adjustments.

4. **Interval Geometry**:
   Timestamps define half-open intervals `[starts_at, ends_at)` using the PostgreSQL `TSTZRANGE` type with inclusive lower and exclusive upper bounds (`'[)'`). Adjacent intervals (e.g., `[10:00, 11:00)` and `[11:00, 12:00)`) share a boundary point but do not overlap, enabling back-to-back bookings on the same resource.

---

## 2. Transaction Isolation and Lock Discipline

The database runs under standard PostgreSQL `READ COMMITTED` transaction isolation with session timezones set to UTC.

### Resource Locking: `FOR SHARE` vs. `FOR UPDATE`

When `insert_confirmed` begins, it selects the target resource:

```sql
SELECT resources.id, resources.name, ...
FROM resources
WHERE resources.id = :resource_id
FOR SHARE
```

1. **Why `FOR SHARE`**:
   A shared row lock (`FOR SHARE`) prevents concurrent transactions from modifying or deleting the parent resource row (e.g., changing its properties or archiving it) while a booking is being attached. Crucially, `FOR SHARE` locks do not conflict with other `FOR SHARE` locks. Competing transactions creating bookings for non-overlapping windows on the same resource can proceed in parallel without blocking one another.

2. **Why Not `FOR UPDATE`**:
   An exclusive row lock (`FOR UPDATE`) would serialize every booking creation on that resource, regardless of whether the requested time intervals collide. This would defeat the concurrent GiST exclusion mechanism, reduce throughput under load, and serialize non-conflicting reservations unnecessarily.

### Strict Lock Ordering

To prevent transaction deadlocks between concurrent operations, locks are acquired in a consistent global hierarchy:

1. **Parent Resource Row**: Acquired `FOR SHARE` via `SELECT ... FOR SHARE` before any booking mutation.
2. **Booking Savepoint Insert**: The booking row is inserted into the session, checking the exclusion constraint.
3. **Outbox Event Row**: Inserted subsequently within the same transaction.

A booking row is never locked or inserted before acquiring the associated resource row lock.

---

## 3. Savepoint Discipline and Exact Conflict Translation

### Nested Savepoints

When executing `insert_confirmed`, the database insert is wrapped in an explicit savepoint via SQLAlchemy's nested transaction context (`async with session.begin_nested():`):

```python
try:
    async with session.begin_nested():
        session.add(booking)
        await session.flush()
except IntegrityError as exc:
    orig = getattr(exc, "orig", exc)
    driver_exc = getattr(orig, "__cause__", None) or orig
    sqlstate = getattr(driver_exc, "sqlstate", None) or getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    constraint_name = getattr(driver_exc, "constraint_name", None) or getattr(orig, "constraint_name", None)
    if sqlstate == "23P01" and constraint_name == "bookings_no_overlap":
        raise SlotConflict("Slot conflict: requested time interval is not available") from exc
    raise
```

### Rationale

1. **Preserving the Outer Transaction**:
   Without a savepoint, a constraint violation in PostgreSQL causes the entire transaction to enter an aborted state (`current transaction is aborted, commands ignored until end of transaction block`). The savepoint ensures that when an exclusion conflict occurs, PostgreSQL rolls back only to the savepoint. The outer transaction remains active, healthy, and capable of executing subsequent queries or committing state (such as recording an idempotency record or audit failure).

2. **Exact Conflict Translation**:
   Only an `IntegrityError` with SQLSTATE `23P01` (exclusion violation) and constraint name `bookings_no_overlap` is translated into the domain-level `SlotConflict` exception. Other integrity errors—such as check constraint violations (`23514`), foreign key errors (`23503`), or unique violations (`23505`)—propagate unaltered. The driver exception attributes are inspected directly without relying on fragile substring matching of error messages.

---

## 4. Service Boundaries and Transaction Ownership

Services operate under strict transaction boundaries:

1. **No Internal Commits**:
   Service methods (`insert_confirmed`, `append_event`) never call `session.commit()`. They receive an already-open transaction on a caller-owned session and perform staged mutations using `await session.flush()`. The caller or transaction coordinator retains exclusive ownership of the commit and rollback lifecycle.

2. **Caller Clock**:
   Services do not query current system time arbitrarily; timestamps (`now`) are supplied by the caller/coordinator clock to ensure deterministic testing and temporal consistency across multi-step flows.

---

## 5. Outbox Event Persistence

Domain events are published via the transactional outbox pattern to guarantee consistency between state transitions and event delivery:

1. **Atomic State and Event Persistence**:
   The event row is appended to the `outbox` table using `append_event` within the same transaction as the booking insert. If the outer transaction commits, both the booking and the event are persisted together. If the transaction rolls back, neither is persisted.

2. **Privacy and Data Redaction**:
   Outbox event payloads contain strictly structural identifiers and timestamps:
   ```json
   {
     "schema_version": 1,
     "booking_id": "<uuid>",
     "recipient_id": "<uuid>",
     "resource_id": "<uuid>",
     "starts_at": "<RFC3339 UTC string>",
     "ends_at": "<RFC3339 UTC string>",
     "expires_at": null
   }
   ```
   No user-generated text, personal names, cancellation reasons, or email addresses are stored in the outbox payload.

3. **Event Scope**:
   Outbox events (`booking_confirmed`) are generated for reservations with an active principal. Administrative blackout intervals (`kind='blackout'`) do not have an associated recipient user and do not append notification events to the outbox.

---

## 6. Idempotency and Atomic Booking Creation

The booking creation endpoint (`POST /api/v1/bookings`) implements atomic, idempotent execution backed by the `idempotency_keys` table.

### Key Acquisition and Mutual Exclusion

Idempotency is scoped per authenticated user (`user_id, key`). Concurrency between duplicate or concurrent requests with the same key is coordinated directly through PostgreSQL row acquisition and locking:

1. **Atomic Insertion**:
   Acquisition attempts an `INSERT INTO idempotency_keys (user_id, key, request_hash, created_at, expires_at)` using `ON CONFLICT (user_id, key) DO NOTHING RETURNING key`. Both `created_at` and `expires_at` are computed from a single sampled `clock_timestamp()` to satisfy the database check constraint `expires_at = created_at + interval '24 hours'`.

2. **Row Locking on Contention**:
   If the insert does not return a row, another transaction holds or previously committed the key. The current transaction executes `SELECT ... FROM idempotency_keys WHERE user_id = :user_id AND key = :key FOR UPDATE`. This blocks until the concurrent holder either commits or rolls back.

3. **Expiry Evaluation**:
   When the lock is acquired, expiry is evaluated against the database clock (`expires_at <= clock_timestamp()`). If the row is expired, it is deleted under the row lock and the acquisition loop retries up to a bounded limit. If unexpired, the stored request hash is validated.

4. **Request Hash and Invariant Validation**:
   - **Hash Match**: The request payload hash is calculated via SHA-256 over canonical UTF-8 JSON containing sorted keys, no whitespace, lowercase normalized UUID path segments, and UTC timestamps with fixed microsecond precision. If the incoming request hash does not match the stored hash, execution aborts with a `422 IDEMPOTENCY_KEY_MISMATCH` error without altering the stored record.
   - **Visible Incomplete Record**: If a committed row has a `NULL` `response_status`, this indicates an invariant violation; the coordinator responds with `503 RETRYABLE_UNAVAILABLE` with `Retry-After: 1`, preventing unearned takeovers.
   - **Replay**: A completed row returns the stored status, body, and selected headers, appending `Idempotency-Replayed: true`.

### Atomic Execution and Persistence

1. **Single Transaction Boundary**:
   The entire booking flow—idempotency key insertion, dynamic window validation, resource locking (`FOR SHARE`), booking insertion (`insert_confirmed`), outbox event generation (`append_event`), and idempotency response update—executes within a single database transaction managed by FastAPI's function-scoped `transaction_dependency`.

2. **Commit Lifecycle**:
   No intermediate commits are performed. If any step fails or raises an uncached error (such as `422 INVALID_WINDOW`, `404 NOT_FOUND`, or unexpected exceptions), the entire transaction rolls back, automatically removing the uncompleted idempotency key row.

3. **Caching Discipline**:
   - Only successfully executed `201 Created` and domain-level `409 Conflict` (`SLOT_CONFLICT`, `RESOURCE_INACTIVE`) responses are cached.
   - Persisted headers are restricted to `Location` and `ETag`. Transport-level identifiers (`X-Request-ID`) are never stored; fresh request IDs are generated by middleware per HTTP request.
   - Replay state (`Idempotency-Replayed`) is computed dynamically per response (`false` on initial creation, `true` on subsequent replays).

4. **Rate Limit Decoupling**:
   Authenticated mutation rate limits (120 requests/user/minute, or 1000 in the race profile) are checked and committed in a separate, short independent transaction before opening the domain transaction. This ensures rate bucket locks are never held concurrently with idempotency or inventory locks.

---

## 7. Concurrency Correctness Gate

The concurrency gate provides automated, high-contention verification of the booking invariant, transactional outbox, and atomic idempotency under real HTTP and database concurrency.

### Scope and Purpose

- **200-User Same-Slot Race (`test_same_slot.py`)**:
  200 distinct authenticated member users with real credentials release simultaneous `POST /api/v1/bookings` requests across an `asyncio.Event` barrier targeting the exact same resource and time slot. Executed across three consecutive rounds on distinct resources and dates. Validates that exactly one request wins (`201 Created`), exactly 199 receive conflict (`409 Conflict` with `SLOT_CONFLICT`), exactly one booking row is persisted, exactly 200 idempotency records are completed, and exactly one outbox event is emitted.
- **Concurrent Same-Key Execution (`test_same_key.py`)**:
  200 identical concurrent requests using the same `Idempotency-Key` from the same user under the race rate limit (`1000/user/min`). Validates that all 200 receive `201 Created` with identical body and ID, exactly one is marked as `Idempotency-Replayed: false`, and 199 are marked `Idempotency-Replayed: true`, persisting exactly one booking and one outbox event. Also validates that conflicting payloads sent concurrently with the same key produce one `201` and one `422 IDEMPOTENCY_KEY_MISMATCH`.
- **Configuration and Environment Defense (`test_race_configuration.py`)**:
  Ensures `TEST_PROFILE=race` is strictly rejected in production environments, verifies rate limit profile thresholds (120 vs 1000), and validates that test token fixtures are cryptographically verified against the server.

### Execution Commands

```bash
# Run concurrency gate with an isolated PostgreSQL 16 database and Uvicorn server
python scripts/verify.py concurrency --fresh

# Run centralized permissions matrix verification
python scripts/verify.py permissions --fresh

# Run full regression including concurrency and permissions gates
python scripts/verify.py regression --fresh
```

### Limitations and Operational Boundary

The concurrency gate is a **deterministic local and CI correctness gate**, not a production load, latency, or throughput benchmark:
- Requests are dispatched over local loopback (`127.0.0.1`) against a single Uvicorn process and ephemeral Docker container.
- It validates transactional invariants, database locking order, and GiST exclusion constraint integrity under severe concurrency contention.
- It does not measure realistic user traffic distributions, wide-area network latency, or multi-node worker behavior. Load testing is evaluated separately via open-loop Locust suites in the dedicated load testing suite.
