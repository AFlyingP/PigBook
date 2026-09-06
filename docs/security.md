# Security Architecture and Specifications

## Overview

This document details the security mechanisms, cryptographic parameters, authorization models, rate limiting strategies, and request handling rules implemented in CommonsBook.

## Password Hashing and Policy

Password storage and verification use Argon2id via `argon2-cffi` with the following parameters:

- **Type**: Argon2id (`Type.ID`)
- **Memory Cost**: 65,536 KiB (64 MiB)
- **Time Cost (Iterations)**: 3
- **Parallelism**: 2 threads
- **Length Constraint**: 12 to 128 Unicode codepoints (measured in codepoints, not bytes; no truncation)
- **Composition**: No arbitrary composition rules enforced to avoid weakening entropy

### Email Normalization
Email addresses are stripped of surrounding whitespace, lowercased, and validated prior to database operations or rate limit hashing. Maximum allowed length is 254 characters.

### Constant-Time User Enumeration Defense
Login requests for non-existent users trigger a dummy Argon2id verification using an internal precomputed hash. This ensures that response timing remains indistinguishable between valid and invalid account names, preventing user enumeration via timing attacks.

## JWT Access Tokens and Authorization

### Access Token Specification
- **Algorithm**: HMAC-SHA256 (`HS256`)
- **Signing Secret**: High-entropy secret key of at least 32 bytes (`JWT_SECRET`). Mandatory in all environments; the application refuses to start or refuses requests if `JWT_SECRET` is unset, empty, or shorter than 32 bytes.
- **Issuer (`iss`)**: `commonsbook`
- **Audience (`aud`)**: `commonsbook-web`
- **Lifetime**: 15 minutes (900 seconds)
- **Required Claims**: `sub` (User UUID), `iat`, `exp`, `jti` (Token UUID)

### Centralized Authorization Layer
All HTTP authorization decisions are centralized in `app.auth.dependencies`.
Route handlers declare an authorization policy using `authorize(policy: Policy)`.
Available policies:
- `public`: Accessible without authentication.
- `authenticated`: Requires an enabled user with member or admin role.
- `admin`: Requires an enabled user with admin role.
- `own_booking`, `own_waitlist`, `metrics`: Reserved for domain resource policies.

### Non-Trust of Token Claims
The `role` claim inside the JWT access token is not trusted for authorization decisions. On every request to an authenticated route:
1. The token signature, issuer, audience, and expiration are verified.
2. The user ID (`sub`) is resolved against the database.
3. The user's active `enabled` status and `role` are reloaded directly from the database.

Any modification to a user's role or account status takes immediate effect, even if an issued access token remains unexpired.

## Refresh Tokens and Cookie Handling

Successful authentication via `POST /api/v1/auth/login` issues an opaque refresh token family row in PostgreSQL and delivers the raw token via an HTTP response cookie:

- **Cookie Name**: `__Secure-commonsbook_rt` in production; `commonsbook_rt` in local and test environments.
- **Flags**: `HttpOnly`, `SameSite=Lax`, `Secure` (true in production; false in local/test).
- **Path**: `/api/v1/auth`
- **Lifetimes**: 7 days rolling expiration, capped at 30 days maximum per family.
- **Cache-Control**: `no-store` header sent on all token responses.

Note: Refresh token rotation (`POST /api/v1/auth/refresh`) and logout revocation routes arrive in T-006.

## Rate Limiting

Rate limiting is enforced atomically in PostgreSQL using the `rate_limits` table (`scope`, `identity_hash`, `window_start`, `count`).

### Independent Transaction Semantics
Rate limits are evaluated and committed in a separate, short transaction before the primary domain transaction begins. As a result:
- Failed login attempts consume rate limit buckets even if the authentication transaction aborts or rolls back.
- Rate bucket locks are never held concurrently with application entity locks (e.g. user, booking, or refresh token locks).
- Buckets are locked in sorted `(scope, identity_hash, window_start)` order to eliminate deadlock risks.

### Identity Hashing
Client IP addresses and normalized emails are hashed using HMAC-SHA256 with `RATE_LIMIT_HMAC_SECRET` before database storage. Raw IP addresses and emails are never logged or persisted in rate limit records. `RATE_LIMIT_HMAC_SECRET` is mandatory in all environments (minimum 32 bytes); the application refuses to start or refuses requests if it is unset, empty, or shorter than 32 bytes.

### Mandatory Secret Configuration
Both `JWT_SECRET` and `RATE_LIMIT_HMAC_SECRET` are strictly required across all deployment environments (including local, test, and production). The service contains no fallback or default secrets and fails closed:
- Missing, empty, or shorter-than-32-byte values are rejected during startup configuration validation in production and whenever set in non-production environments.
- At the point of use (token minting, token verification, and identity hashing), any missing or sub-32-byte secret immediately raises an error, ensuring requests cannot be served with an unconfigured or weak secret.

### Configured Limits
- **Login attempts per IP**: 10 attempts per minute (`login:ip`)
- **Login attempts per email**: 5 attempts per minute (`login:email`)

Exceeding a limit produces HTTP `429 Too Many Requests` with a `RATE_LIMITED` error code and an integer `Retry-After` response header.

### IP Extraction
In production, client IP addresses are resolved from proxy headers using a configured hop count of 1. In local and test environments, the socket peer address is used directly; forwarded headers cannot spoof identities in test environments.

## CORS Configuration

Cross-Origin Resource Sharing (CORS) enforces an exact match against `APP_ORIGIN` without wildcards:
- **Allowed Origins**: Exact `APP_ORIGIN` (e.g. `http://localhost:5173`)
- **Credentials**: Allowed (`true`)
- **Allowed Methods**: `GET`, `POST`, `PATCH`, `DELETE`, `OPTIONS`
- **Allowed Headers**: `Authorization`, `Content-Type`, `Idempotency-Key`, `If-Match`, `X-Request-ID`
- **Exposed Headers**: `ETag`, `Location`, `X-Request-ID`, `Idempotency-Replayed`, `Retry-After`

## Error Envelope and Status Codes

All API error responses follow the standard error envelope:

```json
{
  "error": {
    "code": "<ERROR_CODE>",
    "message": "<Human-readable message>",
    "details": {}
  }
}
```

Request identifiers are communicated exclusively via the `X-Request-ID` HTTP header and are never included in the JSON body. Inbound `X-Request-ID` values on API routes must be valid UUIDs; non-UUID values are replaced with a newly generated UUIDv4.

### Error Codes
- `AUTH_REQUIRED` (401): Missing or malformed authentication credentials on protected routes.
- `INVALID_TOKEN` (401): Invalid signature, expired token, wrong issuer/audience, or disabled user account.
- `INVALID_CREDENTIALS` (401): Incorrect email or password on login; generic message emitted for both missing users and invalid passwords.
- `FORBIDDEN` (403): User lacks the required role for the requested resource or action.
- `VALIDATION_ERROR` (422): Request schema validation failure; field inputs and rejected secrets/passwords are stripped from error details.
- `RATE_LIMITED` (429): Rate limit exceeded; carries `Retry-After` header and integer seconds in details.
- `RETRYABLE_UNAVAILABLE` (503): Database lock timeout or statement cancellation; carries `Retry-After: 1` header.
- `INTERNAL_ERROR` (500): Generic internal error; internal stack traces and server diagnostics are omitted.
