# Operational Runbook

This document describes manual operator procedures for bootstrapping the initial administrator account, performing operator password resets, and auditing administrative and security events.

## 1. Initial Administrator Bootstrap

### Purpose and Invariants
The initial administrator account is provisioned via `scripts/seed.py`.
- **Refusal on Existing Accounts**: The bootstrap tool inspects the database and refuses to run if any user account already exists.
- **Hidden Password Input**: Passwords are never accepted via CLI flags, environment variables, or files. They are read interactively from standard input via `getpass` with terminal echo disabled.
- **No Default Credentials**: There are no fallback or default credentials. Passwords must satisfy the password policy (12 to 128 Unicode codepoints).
- **Audit Logging**: Each bootstrap invocation generates a unique `request_id` (UUID v4) printed to stdout and records an audit log entry in `audit_log` with action `admin.bootstrap`.

### Execution
From the project root:
```bash
python scripts/seed.py --email admin@example.com --display-name "System Administrator"
```
Or using `uv`:
```bash
uv run --project backend python scripts/seed.py --email admin@example.com --display-name "System Administrator"
```

You will be prompted:
```text
Enter administrator password:
```
Upon success, the tool outputs:
```text
Request ID: <uuid>
Administrator successfully created with ID: <user-uuid>
```

### Failure and Refusal Scenarios
1. **Existing User Accounts**:
   - Exit code: 1
   - Error: `Error: Database already contains users. Initial admin bootstrap refused.`
2. **Password Length Out of Range (<12 or >128 codepoints)**:
   - Exit code: 1
   - Error: `Error: Password length must be between 12 and 128 Unicode codepoints (got <N>)`
3. **Invalid Email Address**:
   - Exit code: 1
   - Error: `Error: Invalid email address: ...`

---

## 2. Operator Password Reset

### Purpose and Invariants
When account recovery is required, an operator resets the user's password using `scripts/reset_password.py`.
- **Dry-Run by Default**: Without the `--apply` flag, the script performs a non-mutating inspection: it validates the target user ID and password requirements and reports what would change, writing nothing to the database.
- **Approval File Required on Apply**: When running with `--apply`, the script requires `--approval-file <path>`. The file must contain the exact target user UUID. Missing or mismatched approval files result in immediate refusal.
- **Hidden Input**: New passwords are read from hidden standard input via `getpass`. They are never logged, printed, or exposed via API.
- **Atomic State and Session Revocation**: When applied, the script:
  1. Acquires a row lock on the user record (`SELECT ... FOR UPDATE`).
  2. Updates the Argon2id password hash, increments the user `version`, and updates `updated_at`.
  3. Revokes all active refresh tokens and token families for the user (`revoked_at = now`).
  4. Appends an audit log entry (`action="operator.password_reset"`).
  5. Commits all changes atomically in a single transaction.
- **Audit Correlation**: Each invocation generates a unique `request_id` printed to stdout.

### Procedure

#### Step 1: Dry-Run Inspection
Execute without `--apply`:
```bash
python scripts/reset_password.py <target-user-uuid>
```
Output will confirm target user existence and report active refresh tokens:
```text
Request ID: <request-uuid>
DRY-RUN: Target user found: user@example.com (ID: <target-user-uuid>)
DRY-RUN: Current version: 1, active refresh tokens to revoke: 2
DRY-RUN: No changes made to the database. Provide --apply and --approval-file to apply changes.
```

#### Step 2: Prepare Approval File
Create a local text file containing only the target user UUID:
```bash
echo "<target-user-uuid>" > approval.txt
```

#### Step 3: Apply Password Reset
Execute with `--apply` and `--approval-file`:
```bash
python scripts/reset_password.py <target-user-uuid> --apply --approval-file approval.txt
```
Enter the new password at the prompt:
```text
Enter new password:
Request ID: <request-uuid>
Successfully reset password for user <target-user-uuid> and revoked 2 active refresh tokens.
```

Delete the approval file after use:
```bash
rm approval.txt
```

### Failure and Refusal Scenarios
1. **Missing Approval File on Apply**:
   - Exit code: 1
   - Error: `Error: --approval-file is required when applying changes.`
2. **Approval File Mismatch**:
   - Exit code: 1
   - Error: `Error: Approval file content does not match target user UUID <uuid>.`
3. **Non-Existent Target User**:
   - Exit code: 1
   - Error: `Error: User <uuid> not found.`
4. **Invalid Password Length**:
   - Exit code: 1
   - Error: `Error: Password length must be between 12 and 128 Unicode codepoints (got <N>)`

---

## 3. Audit Correlation

All security-sensitive operations record immutable audit entries in `audit_log`.

### Events Recorded
- `admin.bootstrap`: Initial admin creation via CLI.
- `operator.password_reset`: Password reset and session revocation via CLI.
- `auth.register`: User registration from invitation.
- `admin.invitation_create`: Invitation creation by an administrator.
- `auth.refresh_reuse`: Refresh token reuse detection and family revocation.

### Querying Audit Trail
To correlate a CLI operation with its audit row using the printed `Request ID`:
```sql
SELECT id, actor_id, action, target_type, target_id, request_id, details, created_at
FROM audit_log
WHERE request_id = '<printed-request-uuid>';
```

### Redaction and Security Guarantees
- Raw passwords, password hashes, raw invitation tokens, raw refresh tokens, and cookie values are never stored in `details` or audit logs.
- Audit entries are append-only. Runtime `UPDATE` and `DELETE` operations on `audit_log` are denied.

---

## 4. Background Worker and Maintenance Scheduler

This section details the operation, supervision, cadences, observability, and troubleshooting procedures for the background worker daemon (`backend/app/worker.py`).

### Worker Architecture and Supervision

The worker process runs as an independent asyncio service supervising independent background tasks:
1. **Heartbeat Task**: Updates the primary heartbeat timestamp every 10 seconds.
2. **Hold Expiry Scheduler**: Discovers overdue holds and waiting entries every 30 seconds.
3. **Hourly Maintenance**: Deletes expired idempotency rows and stale rate-limit buckets every 3600 seconds.
4. **Extension Tasks**: Clean registration seam for future background tasks (such as the T-018 notification dispatcher).

#### Session and Connection Invariants
- Each task and operation owns its own `AsyncSession`; sessions are never shared across tasks.
- The worker operates against the shared application database engine (`app.db.session.get_engine`) with its default pool settings.
- Tasks check out a database connection only for the duration of individual short operations and transactions; connections and sessions are never held across `asyncio.sleep` or long awaits.
- Dedicated worker pool sizing (such as a separate engine with `pool_size=2`, `max_overflow=0`) remains to be configured with deployment topology.

#### Graceful Shutdown Budget
- On receiving `SIGTERM` or `SIGINT`, the worker sets its shutdown event, stopping new work across all tasks.
- In-flight work is given up to **25 seconds** (`SHUTDOWN_BUDGET_SECONDS = 25.0`) to complete cleanly before remaining tasks are cancelled.
- If an uncaught exception occurs in any supervised task, the supervisor logs the fatal error, immediately cancels all sibling tasks, and exits non-zero (`sys.exit(1)`), prompting container orchestrator restart.

### Hold Expiry and Promotion (`app/waitlist/scheduler.py`)

#### Operation and Discovery Cadence
- **Cadence**: Runs every 30 seconds.
- **Candidate Discovery**: Discovers resource IDs with overdue offered bookings (`status='offered' AND expires_at <= clock_timestamp()`) or active waiting entries (`status='waiting'`).
- **Bounded Scan and Cursor**: Limits discovery to **100 resources per cycle** using an ascending resource UUID keyset cursor stored in process memory. The cursor advances past every examined resource—including resources skipped because their row locks were unavailable—and wraps to the beginning after reaching the end of the keyset. On process restart, the cursor resets safely to the beginning.
- **Per-Resource Transactions**: Transactions are scoped per resource, not per discovery batch.

#### Row Locking and Expiry Invariants
- For each candidate resource, the worker acquires the resource row `FOR UPDATE SKIP LOCKED`. If the lock cannot be acquired, the resource is skipped (cursor advances) and reconsidered on the next cycle.
- **Clock Authority**: The database clock `clock_timestamp()` is sampled strictly **after** acquiring the resource lock. Pre-wait timestamps are never authoritative.
- **Expiry Transition**: Under the resource lock, due offered bookings (`expires_at <= db_now`) and their linked waitlist entries are transitioned to `status='expired'`, `expires_at` is cleared to `NULL`, and versions are incremented exactly once.
- **Outbox Events**: Exactly one `hold_expired` event (identifier-only schema version 1 payload) is appended to the `outbox` table per expired booking with `aggregate_version` set to the post-change booking version.
- **Promotion**: Calls `promote_waiters(session, resource_id, db_now)` within the same transaction to offer released capacity to the next eligible waiters (applying the 15-minute horizon). All work for the resource commits atomically together.
- **`expire_and_promote` Interface**: Returns the number of offered bookings expired by that call (returns 0 when the call only performed promotion repair or skipped a locked resource).

### Hourly Maintenance (`app/notifications/maintenance.py`)

#### Scope and Deletion Targets
Runs hourly and executes batched deletions in chunks of **500 rows** per transaction until a batch affects fewer than 500 rows:
- **Expired Idempotency Keys**: Deletes rows where `idempotency_keys.expires_at <= clock_timestamp()`.
- **Stale Rate-Limit Buckets**: Deletes buckets where `rate_limits.window_start < clock_timestamp() - interval '24 hours'`.

#### Strict Retention Boundaries
Maintenance deletes **only** expired idempotency keys and old rate-limit buckets. It does **not** delete:
- Bookings (reservations or blackouts)
- User accounts
- Feedback rows
- Audit log entries
- Outbox rows or delivery receipts
- Refresh tokens or refresh token families (retained at least `family_expires_at + 30 days` for reuse detection)

### Observability and Heartbeat Monitoring

Worker health and expiry latency are tracked in the singleton `worker_heartbeat` database table:

```sql
SELECT name, seen_at, expiry_scan_at
FROM worker_heartbeat
WHERE name = 'primary';
```

- **`seen_at`**: Updated at least every **10 seconds** by the worker heartbeat task. Indicates process liveliness.
- **`expiry_scan_at`**: Updated after each complete discovery batch across candidate resources.
- **Maximum Expiry Delay**: Monitored by evaluating the age of `seen_at` and `expiry_scan_at` relative to `clock_timestamp()`. An alert should fire if `seen_at` age exceeds 30 seconds or `expiry_scan_at` age exceeds 90 seconds.

### Troubleshooting and Operational Runbook

#### Symptom: Worker Process Exited Unexpectedly
1. Inspect container logs for `CRITICAL` or `ERROR` messages:
   ```text
   Supervised task '<name>' failed with uncaught exception: ...
   ```
2. Any unhandled error in a background task intentionally halts the process with exit code 1 to ensure failures are visible rather than silently dropped.
3. Verify database connectivity, migration status, and database pool utilization.

#### Symptom: Heartbeat Stale (`seen_at` > 30s)
1. Verify whether the worker container is running:
   ```bash
   docker ps -f "name=worker"
   ```
2. Check database connection limits and active locks:
   ```sql
   SELECT pid, state, query, age(clock_timestamp(), query_start)
   FROM pg_stat_activity
   WHERE application_name LIKE '%asyncpg%' AND state != 'idle';
   ```
3. Restart worker daemon if process is hung or blocked on external resources.

#### Symptom: Holds Not Expiring Promptly (`expiry_scan_at` > 90s)
1. Query overdue offered bookings:
   ```sql
   SELECT id, resource_id, expires_at, clock_timestamp()
   FROM bookings
   WHERE status = 'offered' AND expires_at <= clock_timestamp();
   ```
2. Inspect whether specific resource rows are locked by long-running transactions:
   ```sql
   SELECT locktype, relation::regclass, mode, granted, pid
   FROM pg_locks
   WHERE relation = 'resources'::regclass;
   ```
3. The scheduler safely skips locked resources (`SKIP LOCKED`) and retries on subsequent passes. Long transactions holding resource locks should be investigated and terminated if unprompted.

