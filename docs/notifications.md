# Notification Delivery and Email Provider Configuration

This document specifies the notification delivery architecture, provider configuration contract, and the operator enablement procedure for CommonsBook.

## 1. Architecture and Durability

CommonsBook implements reliable, asynchronous email notification delivery using the transactional outbox pattern:

1. **Transactional Event Creation**:
   - Domain mutations within the core application append event records to the `outbox` table within the caller's database transaction (`app/notifications/outbox.py:append_event`).
   - Supported event types: `booking_confirmed`, `booking_cancelled`, `waitlist_offered`, and `hold_expired`.
   - The aggregate version on the outbox row matches the post-change booking version.
   - Outbox payloads follow the identifier-only schema version 1 specification: they contain UUIDs and timestamps only, with zero user-generated text, personal names, passwords, or recipient email addresses.

2. **Durable Claiming and Leases**:
   - The background worker daemon runs an independent notification dispatcher loop (`app/worker.py:OutboxDispatcher`) polling every 1 second when idle.
   - Rows are claimed using `SELECT ... FOR UPDATE SKIP LOCKED` ordered by `available_at, occurred_at, id`, claiming at most one row per cycle.
   - Claiming transitions the row to `status = 'processing'`, increments `attempts`, assigns a fresh UUID `lease_token`, sets `lease_until = now + 60s`, and commits to PostgreSQL before dispatch.
   - Immediately prior to dispatch, the owned lease is renewed. If ownership was lost, dispatch is skipped.

3. **Idempotency and Receipt Tracking**:
   - Event delivery is handled by `app/notifications/handler.py:dispatch_event`.
   - Before transmission, the handler checks or creates a receipt row in `notification_deliveries` keyed by the unique tuple `(event_id, recipient_id, channel='email')`.
   - If a receipt with `state IN ('sent', 'skipped')` already exists, transmission is skipped and the outbox row is marked `delivered`.
   - Disabled recipients (`users.enabled = false`) or stale/expired waitlist offers are transitioned to `state = 'skipped'` without invoking external network adapters.
   - Every email is sent with a stable RFC 5322 Message-ID: `<event_uuid.recipient_uuid@commonsbook.invalid>`.

4. **Connection Isolation**:
   - Database connections and sessions are never held across SMTP network communication. The database session is committed and closed before invoking the email adapter.
   - Following successful delivery, a short transaction marks the delivery receipt as `'sent'` (recording the provider message ID) and acknowledges the outbox row as `'delivered'`.

5. **Retry, Backoff, and Dead-Lettering**:
   - Failed delivery attempts schedule the row for retry with exponential backoff and deterministic jitter:
     $$\text{available\_at} = \text{now} + \min(3600, 5 \times 2^{\text{attempt}-1}) + (\text{int}(\text{event\_id}) \bmod 5) \text{ seconds}$$
   - When attempts reach 8 (`attempts >= 8`), the row transitions to `status = 'dead'`.
   - The `last_error` field stores only sanitized, redacted error categories (maximum 500 characters), ensuring no SMTP credentials, passwords, or message bodies are ever persisted.
   - Retrying a dead event is performed through the administrative outbox retry endpoint, which is part of the administrative endpoints package and is not yet exposed in this build; until then, dead rows remain in `status = 'dead'`.

---

## 2. Provider Configuration Contract

The notification subsystem is configured via the following environment variables (Spec 9.2):

| Variable | Type | Default | Description |
|---|---|---|---|
| `EMAIL_ADAPTER` | `console` | `smtp` | `console` | Adapter implementation to use. `console` outputs delivery metadata and body to logs in local/test; `smtp` connects to a production SMTP relay over STARTTLS. |
| `EMAIL_ENABLED` | `bool` | `false` | Master toggle for background worker email delivery. When `false`, outbox events remain pending and no email adapter is ever invoked. |
| `SMTP_HOST` | `str` | `""` | SMTP relay server hostname (required when `EMAIL_ADAPTER=smtp` and `EMAIL_ENABLED=true`). |
| `SMTP_PORT` | `int` | `587` | SMTP relay server port (strictly 587 with STARTTLS). |
| `SMTP_USERNAME` | `str` | `""` | Authentication username for the SMTP relay. |
| `SMTP_PASSWORD` | `str` | `""` | Authentication password or API key for the SMTP relay (sensitive credential). |
| `EMAIL_FROM` | `str` | `""` | Validated sender mailbox address (e.g. `notifications@yourdomain.org`). |

---

## 3. Initial Deployment State (`EMAIL_ENABLED=false`)

In accordance with Spec 13.4, email delivery is **disabled by default** on initial deployment:

- **Pending Outbox Rows**: Domain events will be appended to the `outbox` table as reservations, cancellations, and waitlist offers occur. These rows will remain in `status = 'pending'`.
- **Worker Behavior**: The background worker daemon supervises the outbox dispatcher alongside the hold expiry scheduler, heartbeat monitor, and hourly maintenance. When `EMAIL_ENABLED=false`, the dispatcher logs that delivery is disabled and sleeps during each cycle without claiming rows or attempting network connections.
- **Normal Operation**: Pending outbox accumulation during initial deployment prior to provider credential provisioning is normal, expected, and documented.
- **Safety**: No real recipient receives unverified test email, and no provider account or secret exists before the operator runs the enablement checkpoint.

---

## 4. Operator Enablement Procedure (Checkpoint CP-EMAIL)

Prior to enabling live email delivery in a production or staging environment, the human operator must execute the **CP-EMAIL** enablement checkpoint:

### Step 1: Provision an Approved SMTP Provider
The operator provisions an external SMTP service (such as Amazon SES, SendGrid, Mailgun, Postmark, or an organization-approved SMTP relay). The operator provisions the account and supplies the credentials; no automated process registers for external accounts or purchases services.

### Step 2: Configure and Verify Sender Domain
- Configure DNS records (SPF, DKIM, DMARC) for the outbound sending domain to establish email deliverability and avoid spam filtering.
- Verify the outbound mailbox address that will be set in `EMAIL_FROM` (e.g., `notifications@commonsbook.example.com`).

### Step 3: Configure Deployment Secrets
In the deployment environment (e.g., Render Dashboard Environment Variables):
1. Set `EMAIL_ADAPTER=smtp`
2. Set `SMTP_HOST` to the provider relay host (e.g., `email-smtp.us-east-1.amazonaws.com`)
3. Set `SMTP_PORT=587`
4. Set `SMTP_USERNAME` to the SMTP credential username
5. Set `SMTP_PASSWORD` to the secure SMTP secret key
6. Set `EMAIL_FROM` to the verified sender address

### Step 4: Enable Delivery
1. Set `EMAIL_ENABLED=true` in the deployment environment.
2. Restart or redeploy the worker daemon.
3. Inspect worker logs to confirm successful dispatcher startup:
   ```text
   [INFO] worker: OutboxDispatcher: started with SMTPEmailAdapter
   ```
4. The worker will automatically begin claiming due pending outbox events and dispatching them with backoff and receipt tracking.
