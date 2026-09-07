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
