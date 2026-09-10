import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure backend directory is in sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from sqlalchemy import delete, func, select, update

from app.admin.audit import append_audit_log
from app.admin.models import Feedback
from app.auth.models import RefreshToken, User
from app.auth.passwords import hash_password
from app.db.session import get_sessionmaker


async def run_retention(
    *,
    target_scope: str | None = None,
    account_cutoff_days: int = 180,
    feedback_cutoff_days: int = 90,
    apply: bool = False,
    approval_file: str | None = None,
    backup_ref: str | None = None,
) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    account_cutoff = now - timedelta(days=account_cutoff_days)
    feedback_cutoff = now - timedelta(days=feedback_cutoff_days)

    request_id = uuid.uuid4()
    print(f"Request ID: {request_id}")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Find eligible inactive accounts: created before cutoff, not already deleted, not an active admin
        user_stmt = select(User).where(
            User.created_at <= account_cutoff,
            User.role != "admin",
            ~User.email.endswith("@deleted.invalid"),
        )
        res = await session.execute(user_stmt)
        eligible_users = res.scalars().all()
        account_count = len(eligible_users)

        # Find eligible feedback older than feedback_cutoff
        feedback_stmt = select(func.count(Feedback.id)).where(Feedback.created_at <= feedback_cutoff)
        feedback_count = (await session.execute(feedback_stmt)).scalar_one()

        if not apply:
            print(f"DRY-RUN: Eligible accounts for anonymization (older than {account_cutoff_days} days): {account_count}")
            print(f"DRY-RUN: Eligible feedback entries for deletion (older than {feedback_cutoff_days} days): {feedback_count}")
            print("DRY-RUN: No changes made to the database. Provide --apply, --target-scope, --approval-file and --backup-ref to apply.")
            return {"accounts_anonymized": account_count, "feedback_deleted": feedback_count}

        # Apply mode validations: target-scoped and approval-bound
        if not target_scope:
            print("Error: --target-scope is required when applying retention changes.", file=sys.stderr)
            sys.exit(1)

        if not approval_file:
            print("Error: --approval-file is required when applying retention changes.", file=sys.stderr)
            sys.exit(1)

        approval_path = Path(approval_file)
        if not approval_path.exists():
            print(f"Error: Approval file {approval_file} does not exist.", file=sys.stderr)
            sys.exit(1)

        approval_content = approval_path.read_text(encoding="utf-8").strip()
        expected_token = f"APPROVE_RETENTION:{target_scope}"
        if approval_content != expected_token:
            print(
                f"Error: Approval file content does not match required authorization token '{expected_token}'.",
                file=sys.stderr,
            )
            sys.exit(1)

        if not backup_ref:
            print("Error: --backup-ref is required when applying retention changes.", file=sys.stderr)
            sys.exit(1)

    # Perform apply in transaction
    async with sessionmaker() as session:
        async with session.begin():
            # 1. Anonymize accounts
            anonymized_count = 0
            total_feedback_deleted_user = 0
            for u in eligible_users:
                lock_stmt = select(User).where(User.id == u.id).with_for_update()
                locked_u = (await session.execute(lock_stmt)).scalar_one_or_none()
                if locked_u is None or locked_u.email.endswith("@deleted.invalid"):
                    continue

                user_uuid = locked_u.id
                locked_u.email = f"{user_uuid}@deleted.invalid"
                locked_u.display_name = "Deleted participant"
                # Freshly generated random unusable Argon2id digest
                locked_u.password_hash = hash_password(os.urandom(32).hex())
                locked_u.enabled = False
                locked_u.version += 1
                locked_u.updated_at = now

                # Revoke all active refresh tokens for this user
                await session.execute(
                    update(RefreshToken)
                    .where(RefreshToken.user_id == user_uuid, RefreshToken.revoked_at.is_(None))
                    .values(revoked_at=now)
                )

                # Remove feedback associated with this user
                del_fb_res = await session.execute(
                    delete(Feedback).where(Feedback.user_id == user_uuid)
                )
                user_fb_count = int(del_fb_res.rowcount or 0)
                total_feedback_deleted_user += user_fb_count

                anonymized_count += 1

            # 2. Delete feedback older than cutoff
            fb_del_stmt = delete(Feedback).where(Feedback.created_at <= feedback_cutoff)
            fb_del_res = await session.execute(fb_del_stmt)
            total_feedback_deleted = total_feedback_deleted_user + int(fb_del_res.rowcount or 0)

            # 3. Append audit record
            await append_audit_log(
                session,
                action="operator.retention",
                target_type="retention",
                target_id=None,
                actor_id=None,
                request_id=request_id,
                details={
                    "accounts_anonymized": anonymized_count,
                    "feedback_deleted": total_feedback_deleted,
                    "backup_ref": backup_ref,
                    "target_scope": target_scope,
                    "account_cutoff_days": account_cutoff_days,
                    "feedback_cutoff_days": feedback_cutoff_days,
                },
                now=now,
            )

            print(
                f"Successfully applied retention for scope '{target_scope}': {anonymized_count} accounts anonymized, "
                f"{total_feedback_deleted} feedback records deleted. Backup ref: {backup_ref}"
            )
            return {"accounts_anonymized": anonymized_count, "feedback_deleted": total_feedback_deleted}


def main() -> None:
    parser = argparse.ArgumentParser(description="Target-scoped retention and account anonymization tool")
    parser.add_argument("--target-scope", type=str, help="Target scope for retention apply (e.g. 'pilot')")
    parser.add_argument("--account-cutoff-days", type=int, default=180, help="Account age cutoff in days (default: 180)")
    parser.add_argument("--feedback-cutoff-days", type=int, default=90, help="Feedback age cutoff in days (default: 90)")
    parser.add_argument("--apply", action="store_true", help="Apply retention changes (mutating)")
    parser.add_argument("--approval-file", type=str, help="Path to approved approval file")
    parser.add_argument("--backup-ref", type=str, help="Verified backup reference identifier")

    args = parser.parse_args()
    asyncio.run(
        run_retention(
            target_scope=args.target_scope,
            account_cutoff_days=args.account_cutoff_days,
            feedback_cutoff_days=args.feedback_cutoff_days,
            apply=args.apply,
            approval_file=args.approval_file,
            backup_ref=args.backup_ref,
        )
    )


if __name__ == "__main__":
    main()
