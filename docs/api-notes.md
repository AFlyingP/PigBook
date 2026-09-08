# Resource, Availability and Own Booking API

Technical documentation for the resource catalog, availability and own-booking endpoints.

## Endpoints Overview

| Method | Path | Policy | Description |
| --- | --- | --- | --- |
| `GET` | `/api/v1/resources` | `authenticated` | List active resources with pagination |
| `GET` | `/api/v1/resources/{id}` | `authenticated` | Retrieve a single active resource by UUID |
| `GET` | `/api/v1/resources/{id}/availability` | `authenticated` | Query occupied intervals for an active resource |
| `GET` | `/api/v1/bookings` | `own_booking` | List the caller's own reservations |
| `GET` | `/api/v1/bookings/{id}` | `own_booking` | Retrieve one of the caller's own reservations |
| `POST` | `/api/v1/bookings/{id}/cancel` | `own_booking` | Cancel one of the caller's own reservations |

---

## 1. List Resources (`GET /api/v1/resources`)

Retrieves a paginated list of active resources ordered by name ascending, then by ID ascending.

### Authorization

Requires an authenticated member or administrator bearer token (`Policy.authenticated`).

### Query Parameters

| Parameter | Type | Constraints | Default | Description |
| --- | --- | --- | --- | --- |
| `limit` | integer | `1 <= limit <= 100` | `25` | Maximum number of items to return |
| `offset` | integer | `0 <= offset <= 10000` | `0` | Number of items to skip |

Values outside these ranges produce HTTP 422 with error code `VALIDATION_ERROR`.

### Responses

- **HTTP 200 OK**:
  ```json
  {
    "items": [
      {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "name": "Room A",
        "description": "Conference room with projector",
        "location": "Floor 1, West Wing",
        "active": true,
        "version": 1,
        "created_at": "2026-06-01T10:00:00.000000Z",
        "updated_at": "2026-06-01T10:00:00.000000Z"
      }
    ],
    "total": 1,
    "limit": 25,
    "offset": 0
  }
  ```
  Archived resources (`active = false`) are excluded from both `items` and the `total` count.

- **HTTP 401 Unauthorized**:
  - `AUTH_REQUIRED`: Missing or unparseable authentication header.
  - `INVALID_TOKEN`: Expired, forged, or revoked token, or disabled user account.

---

## 2. Get Resource (`GET /api/v1/resources/{id}`)

Retrieves a single active resource by its UUID identifier.

### Authorization

Requires an authenticated member or administrator bearer token (`Policy.authenticated`).

### Path Parameters

| Parameter | Type | Description |
| --- | --- | --- |
| `id` | UUID | Unique identifier of the resource |

### Headers Emitted

- `ETag`: The resource version integer wrapped in literal double quotes, e.g. `"1"`.

### Responses

- **HTTP 200 OK**:
  ```json
  {
    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "name": "Room A",
    "description": "Conference room with projector",
    "location": "Floor 1, West Wing",
    "active": true,
    "version": 1,
    "created_at": "2026-06-01T10:00:00.000000Z",
    "updated_at": "2026-06-01T10:00:00.000000Z"
  }
  ```

- **HTTP 404 Not Found**:
  Returned when the resource does not exist or has been archived (`active = false`). The response payload is identical in both cases to prevent probing of archived inventory:
  ```json
  {
    "error": {
      "code": "NOT_FOUND",
      "message": "Resource not found"
    }
  }
  ```

- **HTTP 401 Unauthorized**:
  - `AUTH_REQUIRED` or `INVALID_TOKEN`.

---

## 3. Get Resource Availability (`GET /api/v1/resources/{id}/availability`)

Queries occupied intervals for an active resource across a specified half-open time window `[starts_at, ends_at)`.

### Privacy Control

Availability responses expose occupied intervals only and never identify who holds them. The `occupied` interval objects contain strictly four fields: `starts_at`, `ends_at`, `kind`, and `status`. No booking IDs, user IDs, emails, display names, created_by references, or cancellation reasons are exposed.

### Authorization

Requires an authenticated member or administrator bearer token (`Policy.authenticated`).

### Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `starts_at` | string | Yes | Start timestamp of the query window (explicit-offset RFC3339) |
| `ends_at` | string | Yes | End timestamp of the query window (explicit-offset RFC3339) |

### Availability Window Rules

The query window must satisfy all of the following rules:
1. Both `starts_at` and `ends_at` parse as explicit-offset RFC3339 strings (e.g. `2026-07-01T10:00:00Z` or `2026-07-01T06:00:00-04:00`). Naive timestamps without timezone offsets are rejected.
2. After normalization to UTC, both timestamps land on a 30-minute boundary with `second == 0` and `microsecond == 0`.
3. `ends_at` is strictly greater than `starts_at`.
4. The window duration (`ends_at - starts_at`) is at most 7 days.

Violation of any of these rules returns HTTP 422 with error code `INVALID_WINDOW`.

### Occupancy Criteria

An occupied interval appears for every booking row where:
- `resource_id` matches the requested resource;
- `status` is either `confirmed` or `offered`. Bookings with status `pending`, `cancelled`, or `expired` do not occupy;
- `time_range && tstzrange(starts_at, ends_at, '[)')` evaluates to true.

Because intervals are half-open (`[start, end)`), adjacent bookings ending exactly at `starts_at` or starting exactly at `ends_at` do not overlap the window and are omitted.

Occupied intervals return the booking's actual half-open range, unclipped by the query window, ordered by `lower(time_range)` ascending, then by `id` ascending.

### Responses

- **HTTP 200 OK**:
  ```json
  {
    "resource_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "starts_at": "2026-07-01T10:00:00.000000Z",
    "ends_at": "2026-07-01T14:00:00.000000Z",
    "timezone": "America/New_York",
    "occupied": [
      {
        "starts_at": "2026-07-01T11:00:00.000000Z",
        "ends_at": "2026-07-01T12:30:00.000000Z",
        "kind": "reservation",
        "status": "confirmed"
      }
    ]
  }
  ```

- **HTTP 404 Not Found**:
  Returned if the resource does not exist or is inactive (`active = false`).
  ```json
  {
    "error": {
      "code": "NOT_FOUND",
      "message": "Resource not found"
    }
  }
  ```

- **HTTP 422 Unprocessable Entity**:
  Returned when the window violates the window rules:
  ```json
  {
    "error": {
      "code": "INVALID_WINDOW",
      "message": "Timestamp must fall on a UTC 30-minute boundary with zero seconds and microseconds"
    }
  }
  ```
  Or for missing parameters / out-of-bounds pagination:
  ```json
  {
    "error": {
      "code": "VALIDATION_ERROR",
      "message": "Request validation failed",
      "details": { ... }
    }
  }
  ```

- **HTTP 401 Unauthorized**:
  - `AUTH_REQUIRED` or `INVALID_TOKEN`.

---

## 4. Own Booking Scope (`own_booking`)

The three booking routes below share one authorization policy. `own` means the
authenticated principal's own reservations, and it means the same thing for members and
administrators: a booking owned by somebody else is reported as `404 NOT_FOUND` even to
an administrator, who reaches other people's bookings through the separate administrative
routes. Blackouts (`kind = "blackout"`) carry no owner and are never visible here, so a
blackout identifier is also `404 NOT_FOUND` on all three routes.

Ownership is resolved in the authorization layer, which reads only the identifiers it
needs and takes no object row lock. Nothing in a request body or a token claim widens
this scope: the acting role is reloaded from the database on every request, and a `role`
claim inside a token is not trusted.

### Rate limits

Reads (`GET`) consume 600 requests per user per minute; the cancellation consumes the
authenticated-mutation budget of 120 requests per user per minute. Exceeding either
returns HTTP 429 `RATE_LIMITED` with an integer `Retry-After` header giving the seconds
remaining in the current window.

The budget is consumed by the authorization layer as soon as the caller is identified —
before the `If-Match` header is parsed, before ownership is resolved and before any domain
work begins. A request that is then rejected with `404`, `428`, `422`, `412` or `409` has
therefore already spent its budget, so probing for other people's booking identifiers is
metered exactly like a successful read. Consumption happens in its own short transaction
that commits independently of the request, so no rate-limit row is ever held while user,
idempotency or inventory locks are taken.

---

## 5. List Own Bookings (`GET /api/v1/bookings`)

Returns the caller's own reservations, most recently created first.

### Query Parameters

| Parameter | Type | Constraints | Default | Description |
| --- | --- | --- | --- | --- |
| `limit` | integer | `1 <= limit <= 100` | `25` | Maximum number of items to return |
| `offset` | integer | `0 <= offset <= 10000` | `0` | Number of items to skip |
| `status` | string | `confirmed`, `offered`, `cancelled`, `expired` | none | Optional status filter |

Values outside these ranges, and any other `status` value, produce HTTP 422 with error
code `VALIDATION_ERROR`. `pending` is a transient in-transaction state that is never
committed, so it is not an accepted filter value.

Results are ordered by `created_at` descending, then by `id` ascending, so paging through
a stable data set never repeats or skips a row.

### Responses

- **HTTP 200 OK**:
  ```json
  {
    "items": [
      {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "resource_id": "9f1c2b7a-1d2e-4c3b-8a9f-0e1d2c3b4a59",
        "user_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
        "kind": "reservation",
        "starts_at": "2026-07-01T11:00:00.000000Z",
        "ends_at": "2026-07-01T12:00:00.000000Z",
        "status": "confirmed",
        "expires_at": null,
        "cancellation_reason": null,
        "version": 1,
        "created_at": "2026-06-01T10:00:00.000000Z",
        "updated_at": "2026-06-01T10:00:00.000000Z"
      }
    ],
    "total": 1,
    "limit": 25,
    "offset": 0
  }
  ```
  `total` counts the caller's matching reservations, not the whole booking table.

- **HTTP 401 Unauthorized**: `AUTH_REQUIRED` or `INVALID_TOKEN`.
- **HTTP 429 Too Many Requests**: `RATE_LIMITED` with a `Retry-After` header.

---

## 6. Get Own Booking (`GET /api/v1/bookings/{id}`)

Retrieves one of the caller's own reservations.

### Headers Emitted

- `ETag`: the booking version wrapped in literal double quotes, for example `"1"`. This
  is the value to send back as `If-Match` when cancelling.

### Responses

- **HTTP 200 OK**: a `Booking` object with the fields shown above.
- **HTTP 404 Not Found**: `NOT_FOUND` for an unknown identifier, a booking owned by
  another user, or a blackout. The response is identical in all three cases, so the route
  cannot be used to probe for other people's bookings. A malformed identifier that is not
  a UUID is `422 VALIDATION_ERROR` with `loc: ["path", "id"]` instead.
- **HTTP 401 Unauthorized**: `AUTH_REQUIRED` or `INVALID_TOKEN`.
- **HTTP 429 Too Many Requests**: `RATE_LIMITED` with a `Retry-After` header.

---

## 7. Cancel Own Booking (`POST /api/v1/bookings/{id}/cancel`)

Cancels one of the caller's own reservations and releases its interval.

### Request

Body (`Cancel`):

```json
{ "reason": "no longer needed" }
```

`reason` is a string of at most 500 characters and defaults to the empty string. Any other
field in the body is rejected with HTTP 422 `VALIDATION_ERROR`; the request carries no
booking identity, status or version of its own.

This route takes no `Idempotency-Key`. Its repeat safety comes from the version in
`If-Match`.

### The `If-Match` precondition

`If-Match` is required and must be a single strong entity tag of the form `"<version>"`,
the value emitted as the `ETag` of the booking:

| Header | Result |
| --- | --- |
| absent or empty | HTTP 428 `PRECONDITION_REQUIRED` |
| `W/"3"`, `*`, `3`, `"03"`, or a list such as `"3", "4"` | HTTP 422 `VALIDATION_ERROR` |
| a version that is no longer current | HTTP 412 `VERSION_MISMATCH` |

The weak form is rejected because `If-Match` uses strong comparison, and `*` is rejected
because the route needs the caller's concrete expected version rather than mere existence
of the booking.

A stale precondition is reported before any state or deadline check, so a caller working
from an out-of-date copy always sees `412` rather than a state-specific answer, and re-reads
before deciding what to do.

### Behavior and error cases

- `confirmed` or `offered` becomes `cancelled`: `expires_at` is cleared, the request's
  `reason` is stored as `cancellation_reason`, the version increments by exactly one, and a
  single `booking_cancelled` event is appended in the same transaction as the update. Eligible
  waitlist offers are created in the same transaction before any remaining released capacity
  becomes visible to a fresh create.
- **HTTP 409 `TOO_LATE`** once the booking has started. The comparison uses the database
  clock, not the application's clock, and the boundary is strict: cancelling is allowed up
  to, but not at, `starts_at`. The clock is sampled after the resource and booking rows
  have been locked, so a request that spent time waiting behind another transaction is
  judged against the moment it actually got the row: a booking that starts during that
  wait is `TOO_LATE`, not cancelled.
- **HTTP 409 `INVALID_STATE`** for a booking in a terminal state other than `cancelled`,
  such as an expired hold.
- **HTTP 200 OK** for a booking that is already `cancelled` when `If-Match` names its
  *current* version. This is the idempotent repeat of a cancellation whose response was
  lost: the stored booking is returned unchanged, with no second event and no version
  increment. The original `cancellation_reason` is kept; a repeat does not overwrite it. A
  repeat that still quotes the pre-cancellation version is stale and returns `412`.
- **HTTP 404 `NOT_FOUND`** for an unknown identifier, another user's booking, or a
  blackout. The `If-Match` precondition is checked before the identifier is resolved, so a
  request that omits `If-Match` is answered `428 PRECONDITION_REQUIRED` — and one that
  sends a malformed `If-Match` is answered `422 VALIDATION_ERROR` — whether or not the
  booking exists. This leaks nothing: the answer is the same for a real foreign booking as
  for an identifier that matches no row at all. A malformed identifier that is not a UUID
  is `422 VALIDATION_ERROR` with `loc: ["path", "id"]`.
- **HTTP 401 Unauthorized**: `AUTH_REQUIRED` or `INVALID_TOKEN`.
- **HTTP 429 Too Many Requests**: `RATE_LIMITED` with a `Retry-After` header.

Successful responses carry the new version as an `ETag` header.

### Concurrency

The cancellation locks the resource row `FOR UPDATE` before re-reading the booking, which
is the same order every inventory-releasing operation uses, and the update itself is
conditional on the expected version. Holding the resource row `FOR UPDATE` ensures that
cancellation and subsequent waitlist promotion commit atomically, creating holds for eligible
waiters before competing fresh creates can take the released capacity. Two simultaneous
cancellations of one booking at the same starting version therefore produce exactly one `200`
and one `412`, one cancelled row at the next version, and exactly one `booking_cancelled`
event. If the transaction fails at any point, the status change, the version increment, waitlist
promotions, and the event roll back together.

### Limitations

There is no reschedule operation: cancel the booking and create a new one. Cancelling a
booking that has already started is not possible through this route. Waitlist promotion on
release is executed atomically within the cancellation transaction before commit; see
`docs/waitlist.md` for queue fairness, keyset scanning, and offer acceptance.
