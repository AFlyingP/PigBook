# Frontend Architecture and User Interface

## 1. Overview and Stack

CommonsBook frontend is a single-page React application built with TypeScript, Vite, React Router v6, TanStack Query v5, and Material UI v5. It implements community resource browsing, availability scheduling, and secure in-memory authentication against the CommonsBook backend REST API.

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

### 2.5 Principal-Scoped Request Recovery (`CreateAttempt`)

- Implemented in `src/api/createAttempt.ts` per Spec 3.4 and 7.3.
- When beginning an attempt, a UUID v4 key and timestamp are generated and associated with the current principal's ID in `sessionStorage`.
- `restoreAttempt(principalId)` enforces that a stored attempt can only be restored if the authenticated principal ID matches.
- Attempts older than 24 hours are automatically expired and purged.
- Clearing a session on sign-out clears stored attempts immediately, preventing cross-account collision.

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
| `/resources/:id` | `ResourceDetail` | Authenticated (Member/Admin) | Detail, locked timezone, 7-day occupancy grid & accessible list |
| `*` | `NotFoundPage` | Public | Accessible 404 catch-all page for unknown or unmounted paths |

Unimplemented routes are handled by the accessible `NotFoundPage` catch-all rather than placeholder screens.

---

## 4. Resource Catalog and Availability UI

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

### 4.4 Booking Launch Contract

- In accordance with the specification, the booking launch contract passes the selected resource and window parameters without mutation.
- The booking entry action is labeled `Book Slot (Feature registration pending)` and disabled until the booking dialog feature is registered.

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
- `tests/auth.test.tsx`: In-memory auth, 401 retry, single-flight refresh, cross-tab BroadcastChannel, CreateAttempt isolation and 24h expiration, LoginForm accessible controls and keyboard operation, RegisterForm fragment erasure, role-guarded layout access control, and exhaustive token persistence checks.
- `tests/resources.test.tsx`: Timezone offset formatting, DST transition wall-clock to UTC calculations (spring forward, fall back, boundary transitions), 7-day window constraints, 30-minute alignment validation, half-open occupancy checking, ResourceList search filtering, ResourceDetail locked timezone, accessible list view, accessible inline booking error validation, and non-mutating launch contract.

### 6.2 Playwright End-to-End Tests

Executed via the test runner against an isolated PostgreSQL database, real Uvicorn API server, and Vite dev server:

```bash
python scripts/verify.py ticket --ticket T-027 --fresh
```

Covers:
- `e2e/auth.spec.ts`: Member sign-in, session restoration on reload, disabled user rejection with 401, single-use invitation fragment registration with URL bar cleanup, logout route protection, multi-tab logout synchronization, and exhaustive storage token absence.
- `e2e/resources.spec.ts`: Catalog browsing with client-side search, resource detail with organization-locked timezone display, 7-day availability schedule, accessible list schedule alternative, disabled booking entry action, and archived resource access handling.
