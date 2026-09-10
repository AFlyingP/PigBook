# CommonsBook Pilot Privacy Notice

**Version:** 2026-09-v1  
**Status:** Pilot Participant Notice  

---

## 1. Operator and Contact Information

- **Pilot Operator**: [To be supplied by operator at Privacy Checkpoint prior to participant recruitment]
- **Contact Email**: [To be supplied by operator at Privacy Checkpoint prior to participant recruitment]
- **Operational Requirement**: An official operator contact address must be populated and verified prior to issuing real invitations to pilot participants. Missing contact details block production invitation distribution as a mandatory quality gate; local integration testing operates on fixture databases without external recruitment.

---

## 2. Information Collected

CommonsBook collects the following categories of data during the invitation-only pilot:

1. **Account and Authentication Information**:
   - Work email address (used for account identification and notifications).
   - Display name (shown within the system).
   - Cryptographic password hash (computed using Argon2id; plaintext passwords are never stored or logged).
   - Cryptographic authentication hashes (SHA-256 rotating refresh token digests and JWT signatures).
2. **Booking and Resource Scheduling Activity**:
   - Resource reservation time intervals, start/end timestamps, kind (reservation or blackout), and current state (confirmed, cancelled, expired, offered).
   - Waitlist memberships and offer acceptance/cancellation transitions.
3. **Consented Pilot Feedback**:
   - Optional survey responses: task completion status, numeric usability rating (1 to 5), difficulty feedback, and suggested improvements.
   - Affirmative consent verification and consent version identifier (`2026-09-v1`).
4. **Security and Operational Telemetry**:
   - Transactional audit log records (action, target type, target identifier, timestamp, and request correlation identifier).
   - Technical connection metadata (client IP for rate limiting, scrubbed error categories).

---

## 3. Purpose of Processing

Data is processed exclusively for:
- Operating the resource scheduling platform, enforcing inventory exclusivity, and managing waitlist queues.
- Security enforcement, rate limiting, abuse detection, and authentication.
- Evaluating usability and gathering empirical pilot performance metrics to improve the platform.

**Exclusions**: Participant data is never sold, leased, shared with advertising networks, or used for behavioral profiling or cross-site tracking.

---

## 4. Third-Party Infrastructure and Data Processors

The pilot runs on the following infrastructure providers:
- **Application and Background Hosting**: Render (container web and worker services).
- **Relational Database**: Managed PostgreSQL 16 on Render (Oregon region).
- **Email Delivery**: Designated pilot SMTP relay provider (transactional booking and waitlist notifications only; no HTML tracking pixels or tracking links).
- **Error and Metrics Telemetry**: Sentry (error reporting with PII scrubbing, headers and query parameters stripped) and Grafana Cloud / Alloy (anonymized Prometheus performance metrics).

---

## 5. Visibility and Privacy Boundaries

- **Other Members**: Cannot view the identities or contact details of booking owners on other reservations. Members see only that a time slot is occupied or unavailable.
- **Administrators**: Authorized organization administrators can view user accounts, system-wide reservation ownership, and aggregate feedback to manage facility operations.
- **Feedback Protection**: Members can only submit their own feedback; individual feedback responses cannot be read by other members.

---

## 6. Retention, Anonymization, and Deletion

1. **Active Pilot Data**: Maintained for the duration of the pilot evaluation.
2. **Feedback Retention**: Raw survey feedback records are retained for **90 days** following pilot conclusion, after which they are deleted by approved maintenance execution.
3. **Account Anonymization**: Inactive pilot accounts and associated profile data are retained for **180 days** before undergoing pseudonymization via `scripts/retention.py`. Anonymization replaces the email address with `<uuid>@deleted.invalid`, sets display name to `Deleted participant`, invalidates password hashes, and revokes all authentication sessions while preserving booking identifiers to maintain database referential integrity.
4. **Database Backups**: Managed database backups are retained in accordance with the host provider's cold backup retention cycle. Data deleted or anonymized in the live database will naturally cycle out of backups according to provider schedules; immediate deletion from historical backup snapshots is not supported.

---

## 7. Participant Rights and Withdrawal

Participants may withdraw from the pilot or request account anonymization and data removal at any time by contacting the operator at the contact address specified above. Upon verified request, operator maintenance tools execute account disabling, session revocation, and data anonymization.

---

## 8. Pilot Disclaimer

This application is deployed as a prototype pilot system. No legal or regulatory compliance certifications (e.g. HIPAA, FERPA, SOC 2) are claimed or implied.
