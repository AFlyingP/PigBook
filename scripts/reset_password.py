import argparse
import asyncio
import getpass
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure backend directory is in sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from sqlalchemy import select, update

from app.admin.audit import append_audit_log
from app.auth.models import RefreshToken, User
from app.auth.passwords import hash_password, validate_password_length
from app.db.session import get_sessionmaker


async def run_reset(user_id_str: str, apply: bool, approval_file: str | None) -> None:
    try:
        target_uuid = uuid.UUID(user_id_str)
    except (ValueError, TypeError):
        print(f"Error: Invalid user UUID: {user_id_str}", file=sys.stderr)
        sys.exit(1)

    # When stdin is redirected or driven programmatically on Windows, ensure getpass reads from stdin
    if not sys.stdin.isatty() and sys.stdin is sys.__stdin__:
        import io
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding=sys.stdin.encoding)

    try:
        new_password = getpass.getpass("Enter new password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nPassword input aborted.", file=sys.stderr)
        sys.exit(1)

    if not new_password:
        print("Error: Password cannot be empty.", file=sys.stderr)
        sys.exit(1)

    try:
        validate_password_length(new_password)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    request_id = uuid.uuid4()
    print(f"Request ID: {request_id}")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Check target user existence
        stmt = select(User).where(User.id == target_uuid)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

        if user is None:
            print(f"Error: User {target_uuid} not found.", file=sys.stderr)
            sys.exit(1)

        # Count active refresh tokens
        rt_stmt = select(RefreshToken).where(
            RefreshToken.user_id == target_uuid,
            RefreshToken.revoked_at.is_(None),
        )
        rt_res = await session.execute(rt_stmt)
        active_tokens = rt_res.scalars().all()
        token_count = len(active_tokens)

        if not apply:
            print(f"DRY-RUN: Target user found: {user.email} (ID: {user.id})")
            print(f"DRY-RUN: Current version: {user.version}, active refresh tokens to revoke: {token_count}")
            print("DRY-RUN: No changes made to the database. Provide --apply and --approval-file to apply changes.")
            return

        # Apply mode requires approval file
        if not approval_file:
            print("Error: --approval-file is required when applying changes.", file=sys.stderr)
            sys.exit(1)

        approval_path = Path(approval_file)
        if not approval_path.exists():
            print(f"Error: Approval file {approval_file} does not exist.", file=sys.stderr)
            sys.exit(1)

        approval_content = approval_path.read_text(encoding="utf-8").strip()
        if approval_content != str(target_uuid):
            print(
                f"Error: Approval file content does not match target user UUID {target_uuid}.",
                file=sys.stderr,
            )
            sys.exit(1)

        # In apply mode: Lock user FOR UPDATE before revoking token rows
        lock_stmt = select(User).where(User.id == target_uuid).with_for_update()
        lock_res = await session.execute(lock_stmt)
        locked_user = lock_res.scalar_one_or_none()
        if locked_user is None:
            print(f"Error: User {target_uuid} not found during lock acquisition.", file=sys.stderr)
            sys.exit(1)

        now = datetime.now(timezone.utc)
        new_hash = hash_password(new_password)
        locked_user.password_hash = new_hash
        locked_user.version += 1
        locked_user.updated_at = now

        # Revoke all refresh families for that user
        rev_stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.user_id == target_uuid,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        rev_res = await session.execute(rev_stmt)
        revoked_count = rev_res.rowcount

        # Append audit record
        await append_audit_log(
            session,
            action="operator.password_reset",
            target_type="user",
            target_id=target_uuid,
            actor_id=None,
            request_id=request_id,
            details={
                "user_id": str(target_uuid),
                "revoked_count": revoked_count,
            },
            now=now,
        )

        await session.commit()
        print(f"Successfully reset password for user {target_uuid} and revoked {revoked_count} active refresh tokens.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Operator password reset CLI")
    parser.add_argument("user_id", nargs="?", help="Target user UUID")
    parser.add_argument("--user-id", dest="opt_user_id", help="Target user UUID")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply password reset (defaults to dry-run mode without this flag)",
    )
    parser.add_argument(
        "--approval-file",
        help="Path to approval file whose content matches target user UUID",
    )
    args = parser.parse_args()

    user_id_val = args.opt_user_id or args.user_id
    if not user_id_val:
        parser.error("A target user UUID must be provided.")

    asyncio.run(
        run_reset(
            user_id_str=user_id_val,
            apply=args.apply,
            approval_file=args.approval_file,
        )
    )


if __name__ == "__main__":
    main()
