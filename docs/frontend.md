# Frontend Architecture and User Interface

## 1. Overview and Stack

CommonsBook frontend is a single-page React application built with TypeScript, Vite, React Router v6, TanStack Query v5, and Material UI v5. It implements community resource browsing, availability scheduling, member reservation creation, uncertain result recovery, own-booking management with optimistic concurrency cancellation, waitlist joining, and server-deadline offer countdown and acceptance.

### Stack Components

- **Runtime & Build**: Node.js 22, Vite 6, TypeScript 5.7
- **UI Framework**: React 18, Material UI (MUI) v5, Emotion
- **State Management**: TanStack Query v5 (server state cache), React Context (in-memory authentication)
- **Routing**: React Router v6 (`src/routes.tsx`)
- **Type Generation**: `openapi-typescript` generating types from `docs/openapi.json`
- **Test Frameworks**: Vitest 3 with Testing Library for unit/component tests, Playwright with Chromium for end-to-end browser verification

---

## 2. Security and Authentication Architecture

The authentication client conforms to Spec 3.4, 7.1, 7.2, and 8.1.

### 2.1 In-Memory Token Storage

- Access JWTs exist strictly in browser memory (`inMemoryAccessToken` variable in `src/api/client.ts`).
- Bearer tokens are **never** persisted to `localStorage`, `sessionStorage`, cookies, or application logs.
- When an API request is made, `request<T>()` injects the header `Authorization: Bearer <token>`.

### 2.2 Cookie-Based Refresh and One-Shot Retry

- Session continuation relies on an `HttpOnly`, `SameSite=Lax` rotating refresh cookie (`commonsbook_rt` in local/test, `__Secure-commonsbook_rt` in production) scoped to `/api/v1/auth`.
- When an authenticated request encounters HTTP 401, `request<T>()` automatically initiates `refreshSession()` and retries the request once with the new token.
- If refresh fails (e.g. revoked family or disabled user), the session is cleared, in-flight attempts are wiped, and the user is transitioned to the unauthenticated state.

### 2.3 Single-Flight and Cross-Tab Coordination

- **Single Tab**: Simultaneous 401 responses within the same browser tab are serialized into a single-flight Promise. All concurrent requests wait on the same refresh call.
- **Cross-Tab Synchronization**: When available, `navigator.locks.request('commonsbook-refresh')` ensures only one browser tab executes the rotating refresh call at a time.
- Upon successful refresh or logout, state is shared across tabs via a `BroadcastChannel('commonsbook-auth')`:
  - `TOKEN_REFRESHED`: updates in-memory token and current user in other tabs.
  - `LOGOUT`: immediately clears in-memory state and query caches in all open tabs.

### 2.4 Invitation Fragment Handling

- Registration links use URL fragments: `/register#token=<opaque_token>`.
- On component mount, the invitation token is read exactly once from `window.location.hash` and immediately removed from the browser address bar via `window.history.replaceState`.
- The raw token is retained only in component state during form submission and is never written to persistent browser storage or logs.

### 2.5 Principal-Scoped Request Recovery (`CreateAttempt`) and 24-Hour Gating

- Implemented in `src/api/createAttempt.ts` per Spec 3.4 and 7.3.
- When beginning a booking or blackout create attempt, a UUID v4 idempotency key and creation timestamp are recorded alongside the active principal ID in `sessionStorage`.
- Draft storage contains only identifiers and timestamps—never authentication tokens, passwords, or credentials.
- `restoreAttempt(principalId)` enforces that a stored attempt can only be restored if the authenticated principal ID matches; principal mismatch or corrupt draft data clears storage immediately.
- **Spec 7.3 24-Hour Rule**: The stored draft preserves `createdAt` intact so the UI dialog enforces the lifecycle:
  - Under 24 hours: Automatic replay via same-key retry is available on timeout, network interruption, or HTTP 503.
  - At or over 24 hours: Automatic replay stops. The user is presented with a warning and a "Check My Bookings" action before starting an explicit new attempt with a fresh idempotency key.
- Clearing a session on sign-out wipes stored attempts immediately, ensuring cross-account isolation.

---

## 3. Routes and Navigation

Routes are centralized exclusively in `src/routes.tsx`:

| Path | Component / Feature | Access Policy | Description |
|---|---|---|---|
| `/` | `LandingPage` | Public | Project introduction, privacy link, sign-in CTA, demo info |
| `/login` | `LoginPage` | Public | Email/password sign-in form, password reveal, accessible errors |
| `/register` | `RegisterPage` | Public (with token fragment) | Invitation registration form |
| `/privacy` | `PrivacyPage` | Public | Plain-language versioned privacy notice (Spec 12.2) |
| `/resources` | `ResourceList` | Authenticated (Member/Admin) | Resource catalog, search, pagination |
| `/resources/:id` | `ResourceDetail` | Authenticated (Member/Admin) | Detail, locked timezone, 7-day occupancy grid, booking & waitlist dialogs |
| `/my-bookings` | `MyBookingsPage` | Authenticated (Member/Admin) | Own reservations table, status filter, optimistic concurrency cancel |
| `/waitlist` | `WaitlistPage` | Authenticated (Member/Admin) | Own waitlist entries, active offer cards with countdown, accept/decline |
| `/feedback` | `FeedbackPage` | Authenticated (Member/Admin) | Consented 4-question feedback survey with affirmative consent requirement |
| `/admin/resources` | `AdminResourcesPage` | Admin Only | Inventory catalog, create/edit dialogs, soft archive (Spec 7.1, 4.2 E17-E20) |
| `/admin/resources/:id/blackouts` | `AdminBlackoutsPage` | Admin Only | Blackout schedule table, idempotent creation, cancellation (E21-E23) |
| `/admin/bookings` | `AdminBookingsPage` | Admin Only | Booking filter table, reservation details, cancel with reason (E24-E26) |
| `/admin/users` | `AdminUsersPage` | Admin Only | User directory, role promotion/demotion, enable/disable, invite dialog (E27-E29) |
| `/admin/audit` | `AdminAuditPage` | Admin Only | Immutable system audit log table with target ID filtering (E30) |
| `/admin/outbox` | `AdminOutboxPage` | Admin Only | Outbox event monitoring and dead-only retry confirmation (E31-E32) |
| `/admin/feedback` | `AdminFeedbackPage` | Admin Only | Consented survey responses viewer restricted to authorized fields (E34) |
| `*` | `NotFoundPage` | Public | Accessible 404 catch-all page for unknown or unmounted paths |

Unimplemented routes are handled by the accessible `NotFoundPage` catch-all rather than placeholder screens.

---

## 4. Resource Catalog, Booking and Waitlist UI

### 4.1 Resource Browsing & Search

- `ResourceList` fetches paginated resources (`GET /api/v1/resources?limit=12&offset=...`).
- Client-side search filters the current page by name, location, and description.
- Search result counts are announced to screen readers via an `aria-live="polite"` status element.
- Loading states display animated MUI Skeletons; empty states provide guidance and query reset actions.

### 4.2 Timezone Display and Alignment

- All availability and reservation times are stored and transmitted in UTC with RFC3339 format (`Z` offset).
- The UI displays times localized to `America/New_York` with visible offset (e.g. `America/New_York (UTC-04:00)`).
- Organization timezone selector is fixed/locked to reflect facility location.
- Start and end instants are validated to fall on UTC 30-minute boundaries (minutes 00 or 30, seconds 00).
- Booking duration is validated between 30 minutes and 4 hours inclusive, with start time at least 15 minutes in the future and within 90 days.

### 4.3 Seven-Day Availability Grid & Accessible List

- `ResourceDetail` loads a 7-day window calculated by `getSevenDayWindow(startDateStr)`.
- Slot UTC timestamps on DST transition days (spring-forward 23h and fall-back 25h) are computed DST-aware using New York wall-clock conversion, with maximum window duration strictly clamped to 7 days (168 hours) to satisfy backend validation.
- Half-open interval occupancy (`[starts_at, ends_at)`) ensures adjacent bookings do not produce false overlaps.
- **Accessible List Alternative**: Users can toggle between the visual grid and an accessible list view (`<section aria-label="Accessible 7-day availability schedule">`). In accordance with Spec 7.1 and US-02, occupied intervals display slot kind and status without revealing owner identity.

### 4.4 Booking Dialog, Idempotency and Uncertain Recovery (Spec 4.3, 7.2, 7.3)

- `BookingDialog` handles reservation submissions to `POST /api/v1/bookings` with an `Idempotency-Key` UUID v4 header.
- The attempt is recorded in `sessionStorage` and held until a definitive result is reached:
  - **Definitive 201 Created**: Clears attempt, invalidates query cache for `['resources']` and `['bookings', 'mine']`, displays confirmation message, and closes dialog. No optimistic success state is assumed.
  - **Definitive 409 Conflict**: Clears attempt, invalidates `['resources']` availability, displays conflict alert (e.g. `SLOT_CONFLICT` or `RESOURCE_INACTIVE`), and offers an action to join the waitlist for the slot. Requires an explicit new attempt with a fresh idempotency key.
  - **Definitive 422 Validation Error**: Clears attempt and renders readable field-specific errors parsed defensively from the `details` envelope without displaying raw `[object Object]`.
  - **Uncertain Result (Network Error, Timeout, HTTP 503)**: Retains the exact payload and idempotency key. Displays a "Retry Booking" action that resends the identical key without rotation. On page reload, restores the uncertain attempt for the matching principal.
  - **Abandonment Warning**: If the user chooses to abandon an uncertain attempt, a prominent warning advises checking existing bookings because earlier requests may have succeeded on the server.

### 4.5 Own Bookings Management and Optimistic Concurrency Cancellation (Spec 4.1, 4.2 E10-E12, 7.2)

- `OwnBookingsTable` lists the user's reservations (`GET /api/v1/bookings`) with pagination and status filtering (`confirmed`, `offered`, `cancelled`, `expired`).
- All booking times are displayed in the organization timezone (`America/New_York`).
- **Cancellation Flow**:
  - Requires user confirmation via an accessible modal dialog with an optional cancellation reason field.
  - Queries `GET /api/v1/bookings/{id}` to obtain the strong `ETag` version before sending the mutation.
  - Submits `POST /api/v1/bookings/{id}/cancel` with `If-Match: "<version>"`.
  - **HTTP 412 Handling**: If a version mismatch occurs, refetches latest booking details and alerts the user that another update took place without silently overwriting.
  - **HTTP 409 Handling**: Displays accurate terminal status messages (e.g. `TOO_LATE` when the cancellation cutoff has passed).
  - Successful cancellation invalidates `['bookings', 'mine']`, `['resources']`, and `['waitlist', 'mine']`.

### 4.6 Waitlist Joining, Server Deadline Offer Countdown and Acceptance (Spec 7.1, 7.2, 7.3, 11.5)

- `WaitlistDialog` submits `POST /api/v1/waitlist` with resource ID and desired window.
  - `409 SLOT_AVAILABLE`: slot became free; refreshes availability and offers direct booking.
  - `409 WAITLIST_FULL`: friendly capacity message with no claim of having joined.
- `OfferCard` manages active offers:
  - Waitlist entry carries `offered_booking_id`. The component fetches `GET /api/v1/bookings/{offered_booking_id}` (E11) to obtain the server `expires_at` deadline.
  - Renders a live countdown timer (`MM:SS`) from the server deadline.
  - At deadline (00:00), automatically refetches server state; the local timer never declares a terminal state locally without server confirmation.
  - **Offer Acceptance**: Submits `POST /api/v1/waitlist/{id}/accept` with `If-Match` matching the **waitlist entry version** (not the booking version). Confirmed booking retains the identical booking ID.
  - **Offer Decline**: Confirms before submitting `DELETE /api/v1/waitlist/{id}` with entry version `If-Match`.
  - Successful changes invalidate `['waitlist', 'mine']`, `['bookings', 'mine']`, and `['resources']`.

---

### 4.7 Administrator Inventory, Blackouts, Operations and Feedback UI (Spec 7.1, 7.2, 8.3, 12.2)

#### 4.7.1 Administrator Inventory and Soft Archive (Spec 7.1, 4.2 E17-E20)
- `ResourceTable` provides paginated browsing of resources (`GET /api/v1/admin/resources`) with status filtering (all, active only, archived only).
- **Resource Creation (E18)**: `ResourceCreateDialog` submits name (1..100), location (1..200), and optional description (0..2000). Inline errors are announced via accessible live regions.
- **Resource Editing (E19)**: `ResourceEditDialog` submits PATCH with `If-Match: "<version>"`. Handles HTTP 412 (`VERSION_MISMATCH`) by refetching latest server state and warning the administrator, avoiding silent overwrites. Handles HTTP 409 (`RESOURCE_IN_USE`) as a visible, non-destructive alert when attempting to deactivate a resource with future bookings or waitlist entries.
- **Soft Archive (E20)**: `ResourceArchiveDialog` sends DELETE with `If-Match`. Deactivates the resource (`active=false`). There is strictly **no hard-delete or forced-cancel** control anywhere in the application.

#### 4.7.2 Blackout Schedule & Idempotency Key Recovery (Spec 7.1, 7.2, 7.3, 4.2 E21-E23)
- `BlackoutTable` lists scheduled blackout windows for a specific resource (`GET /api/v1/admin/resources/{id}/blackouts`).
- **Blackout Creation (E22)**: `BlackoutCreateDialog` initiates an attempt with `beginAttempt(principalId, "blackout", payload)`, generating a UUID v4 `Idempotency-Key`.
  - Definitive 201/409/422 responses clear the stored attempt.
  - HTTP 409 (`SLOT_CONFLICT` / `RESOURCE_INACTIVE`) is a completed result that refreshes the blackout list and requires a fresh attempt with a new key.
  - Transport failures, network timeouts, or HTTP 503 keep the stored attempt in `sessionStorage` and display a "Retry with Same Key" action.
  - Restored attempts enforce the Spec 7.3 24-hour limit: attempts older than 24 hours disable automatic replay and direct the administrator to check the schedule before initiating a new attempt.
- **Blackout Cancellation (E23)**: `BlackoutCancelDialog` requires explicit confirmation and sends DELETE with `If-Match`.

#### 4.7.3 Administrator Booking Operations (Spec 7.1, 8.3, 4.2 E24-E26)
- `AdminBookingsTable` lists reservations across all resources (`GET /api/v1/admin/bookings`) with parameterized filters for status, resource ID, user ID, and a paired start/end timestamp range (enforcing max 90-day span and simultaneous provision).
- **Reservation Details (E25)**: `AdminBookingDetailDialog` displays booking attributes, lifecycle timestamps, and cancellation reasons.
- **Reservation Cancellation (E26)**: `AdminBookingCancelDialog` sends POST with `If-Match` and an optional reason (max 500 characters). Accurately surfaces 409 `TOO_LATE` and `INVALID_STATE`, and refetches on 412.

#### 4.7.4 User Directory, Roles & Single-Use Invitations (Spec 7.1, 8.3, 4.2 E27-E29)
- `UserTable` lists users with enabled status filtering. Allows role promotion/demotion and account enable/disable via PATCH E28.
- **Last Administrator Protection**: Both demotion and deactivation requests surface HTTP 409 `LAST_ADMIN` accurately and preserve the existing role and enabled status in the view.
- **Copy-Once Invitations (E29)**: `InvitationDialog` generates invitations with assigned roles. Displays `invitation_url` strictly once in component state, provides a copy action, and immediately clears the link when the dialog closes. Invariants: invitation tokens are never stored in browser storage (`localStorage` / `sessionStorage`), never logged, and never transmitted to telemetry.

#### 4.7.5 Audit Log & Transactional Outbox (Spec 7.1, 8.3, 4.2 E30-E32)
- `AuditTable` displays immutable administrative and security audit entries with target ID filtering. Server payloads are rendered strictly as text.
- `OutboxTable` monitors background notification events with status filtering (pending, processing, delivered, dead).
- **Dead-Item Retry (E32)**: Retry action is strictly offered only for events in `dead` status and requires explicit confirmation. Submits `POST /api/v1/admin/outbox/{id}/retry` and surfaces HTTP 409 `INVALID_STATE` if the event state changed.

#### 4.7.6 Feedback Survey & Anonymized Admin View (Spec 7.2, 12.2, 4.2 E33-E34)
- **Member Survey (`/feedback`)**: 4-question survey: rating (1..5), task completed (boolean), optional difficulty (0..2000), optional improvement (0..2000), and consent version (`2026-09-v1`).
  - Consent checkbox is strictly **unchecked on first render**.
  - Direct link to `/privacy`.
  - HTTP 422 `CONSENT_REQUIRED` is displayed inline without losing any entered text.
- **Admin Feedback Viewer (`/admin/feedback`)**: Displays only authorized Spec 4.1 fields (`id`, `user_id`, `rating`, `task_completed`, `difficulty`, `improvement`, `consent_version`, `created_at`). Displays no user identities beyond pseudonymous user IDs. All free text is rendered strictly as text to prevent markup injection.

#### 4.7.7 Accessibility, Responsive Layout & Telemetry Safeguards
- **WCAG 2.2 AA Baseline**: Visible focus indicators (`:focus-visible` outlines), focus trapping inside modal dialogs and automatic focus restoration to the triggering control upon dismissal (Escape or Cancel button), non-colour-only status badges, polite live regions (`aria-live="polite"`), accessible labels for all enabled interactive controls, and global reduced-motion support via `MuiCssBaseline` (`@media (prefers-reduced-motion: reduce)`) suppressing animation and transition durations.
- **Contrast Compliance**: Color pairs derived directly from `src/theme.ts` palette tokens and verified against live computed styles satisfy WCAG 2.2 AA >= 4.5:1 contrast ratios (normal text) via relative-luminance calculations.
- **Mobile Viewport**: Full responsiveness on mobile viewports (~375px wide) achieved through natural flex-wrapping and semantic layouts without relying on global `overflow-x: hidden` clipping on main layout containers. Bounding boxes are verified not to exceed viewport boundaries.
- **Telemetry Safeguard**: Synthetic user journeys verify that no request is made to Sentry or any third-party telemetry host, and no secrets (tokens, cookie values, invitation links) leak in outbound requests. Full telemetry instrumentation belongs to T-034.

---

## 5. OpenAPI and Generated Types

### 5.1 Deterministic OpenAPI Export

The OpenAPI specification is generated directly from the FastAPI application routes:

```bash
uv run --project backend python -c "import sys, json; sys.path.insert(0, 'backend'); from app.main import create_app; from pathlib import Path; schema = create_app().openapi(); Path('docs/openapi.json').write_text(json.dumps(schema, indent=2, sort_keys=True) + chr(10), encoding='utf-8')"
```

### 5.2 TypeScript Code Generation

TypeScript types are generated from `docs/openapi.json` into `frontend/src/api/schema.d.ts`:

```bash
npx --prefix frontend openapi-typescript docs/openapi.json -o frontend/src/api/schema.d.ts
```

---

## 6. Verification and Testing

### 6.1 Vitest Unit and Component Tests

Run component and utility tests:

```bash
npm --prefix frontend test -- --run
```

Covers:
- `tests/auth.test.tsx`: In-memory auth, 401 retry, single-flight refresh, cross-tab BroadcastChannel, CreateAttempt isolation and draft preservation, LoginForm accessible controls and keyboard operation, RegisterForm fragment erasure, role-guarded layout access control, and exhaustive token persistence checks.
- `tests/resources.test.tsx`: Timezone offset formatting, DST transition wall-clock to UTC calculations (spring forward, fall back, boundary transitions), 7-day window constraints, 30-minute alignment validation, half-open occupancy checking, ResourceList search filtering, ResourceDetail locked timezone, accessible list view, accessible inline booking error validation, and non-mutating launch contract.
- `tests/bookings.test.tsx`: Booking creation with UUID v4 idempotency keys, draft persistence, definitive 201/409/422 outcomes, uncertain network failure and 503 same-key retry, reload attempt recovery, Spec 7.3 24-hour gating, cross-account attempt isolation, own bookings listing with status filtering, cancel confirmation with strong ETag `If-Match`, and 412 version mismatch refetch handling.
- `tests/waitlist.test.tsx`: Waitlist joining outcomes (201, 409 SLOT_AVAILABLE, 409 WAITLIST_FULL capacity message), E11 booking fetch for offer deadline, countdown timer from server deadline, accept with waitlist entry version in `If-Match` (distinct from booking version), expired countdown server refetch without local state assumption, and decline with entry version.
- `tests/adminInventory.test.tsx`: Resource creation with validation, version-pinned editing with 412 VERSION_MISMATCH refetch prompt, 409 RESOURCE_IN_USE visible handling, soft archive verification with absence of hard-delete controls, blackout creation with shared UUID v4 idempotency key, uncertain 503 same-key retry, definitive 409 key clearing, blackout cancellation with If-Match, and non-colour-only status text.
- `tests/adminOperations.test.tsx`: Administrator bookings filtering, details modal, cancellation with reason and strong ETag `If-Match`, 412 version mismatch warning, 409 TOO_LATE handling, LAST_ADMIN active protection on user role demotion and account disabling, copy-once invitation link generation with state clearing on close and storage absence, audit log target filtering, outbox monitoring with dead-only retry confirmation, and member route denial.
- `tests/feedback.test.tsx`: Member feedback survey with unchecked consent on initial render, affirmative consent requirement, inline error presentation without text loss on 422 CONSENT_REQUIRED, field bounds validation, submission payload shape with consent_version, plain-text rendering of freeform responses, and admin feedback table restricting fields to Spec 4.1 without user identity leakage.

### 6.2 Playwright End-to-End Tests

Executed via the verification runner against an isolated PostgreSQL database, real Uvicorn API server, and Vite dev server:

```bash
python scripts/verify.py ticket --ticket T-031 --fresh
```

Covers:
- `e2e/auth.spec.ts`: Member sign-in, session restoration on reload, disabled user rejection with 401, single-use invitation fragment registration with URL bar cleanup, logout route protection, multi-tab logout synchronization, and exhaustive storage token absence.
- `e2e/resources.spec.ts`: Catalog browsing with client-side search, resource detail with organization-locked timezone display, 7-day availability schedule, accessible list schedule alternative, selecting slot to open booking dialog without premature mutation, and archived resource access handling.
- `e2e/bookings.spec.ts`: End-to-end booking creation with idempotency key, duplicate click protection, own bookings listing, and two-session concurrent booking conflict (one 201, one SLOT_CONFLICT, exactly one active reservation).
- `e2e/waitlist.spec.ts`: Two-member book/join/cancel/offer/accept lifecycle verifying the identical booking ID transitions to confirmed for the waiting member, and isolated hold expiry test seam manipulating only the test database.
- `e2e/adminInventory.spec.ts`: Administrator resource creation, editing, and soft archive; blackout window creation and cancellation on an active resource; concurrent-edit 412 refresh warning; and member route access denial with 403 Forbidden alert.
- `e2e/adminOperations.spec.ts`: Administrator reservation filtering and cancellation with reason; user role management with last-active administrator protection (409 LAST_ADMIN); single-use invitation link dialog with copy action and storage absence verification; audit log review; dead outbox event retry confirmation using isolated test fixture seam; and member access denial across operations routes.
- `e2e/accessibility.spec.ts`: WCAG 2.2 AA contrast ratio verification for theme color pairs; mobile-width viewport (375px) horizontal overflow checks across all 13 critical routes; keyboard navigation with focus visible indicators and focus trapping/restoration on modal dialogs; live region announcements and non-colour-only status badges; synthetic-user journey screenshot capture into EVIDENCE_DIR; and verification that zero requests reach Sentry or third-party telemetry hosts with zero credential leakage.
