# Resource Catalog and Availability API

Technical documentation for the resource catalog and availability endpoints.

## Endpoints Overview

| Method | Path | Policy | Description |
| --- | --- | --- | --- |
| `GET` | `/api/v1/resources` | `authenticated` | List active resources with pagination |
| `GET` | `/api/v1/resources/{id}` | `authenticated` | Retrieve a single active resource by UUID |
| `GET` | `/api/v1/resources/{id}/availability` | `authenticated` | Query occupied intervals for an active resource |

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
