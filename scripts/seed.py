import argparse
import asyncio
import getpass
import sys
import uuid
from pathlib import Path

# Ensure backend directory is in sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from sqlalchemy import func, select

from app.admin.audit import append_audit_log
from app.auth.models import User
from app.auth.passwords import hash_password, normalize_email, validate_password_length
from app.db.session import get_sessionmaker


async def run_seed(email: str, display_name: str) -> None:
    # When stdin is redirected or driven programmatically on Windows, ensure getpass reads from stdin
    if not sys.stdin.isatty() and sys.stdin is sys.__stdin__:
        import io
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding=sys.stdin.encoding)

    # Read password from hidden stdin
    try:
        password = getpass.getpass("Enter administrator password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nPassword input aborted.", file=sys.stderr)
        sys.exit(1)

    if not password:
        print("Error: Password cannot be empty.", file=sys.stderr)
        sys.exit(1)

    try:
        validate_password_length(password)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        norm_email = normalize_email(email)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    cleaned_display_name = display_name.strip()
    if not (1 <= len(cleaned_display_name) <= 80):
        print("Error: Display name must be between 1 and 80 characters.", file=sys.stderr)
        sys.exit(1)

    request_id = uuid.uuid4()
    print(f"Request ID: {request_id}")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Check if any user already exists
        count_stmt = select(func.count(User.id))
        res = await session.execute(count_stmt)
        user_count = res.scalar_one()

        if user_count > 0:
            print("Error: Database already contains users. Initial admin bootstrap refused.", file=sys.stderr)
            sys.exit(1)

        password_hash = hash_password(password)
        admin_id = uuid.uuid4()
        admin_user = User(
            id=admin_id,
            email=norm_email,
            password_hash=password_hash,
            display_name=cleaned_display_name,
            role="admin",
            enabled=True,
            version=1,
        )
        session.add(admin_user)

        await append_audit_log(
            session,
            action="admin.bootstrap",
            target_type="user",
            target_id=admin_id,
            actor_id=admin_id,
            request_id=request_id,
            details={
                "email": norm_email,
                "role": "admin",
            },
        )

        await session.commit()
        print(f"Administrator successfully created with ID: {admin_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Initial administrator bootstrap CLI")
    parser.add_argument("--email", "-e", required=True, help="Administrator email address")
    parser.add_argument("--display-name", "-d", required=True, help="Administrator display name")
    args = parser.parse_args()

    asyncio.run(run_seed(email=args.email, display_name=args.display_name))


if __name__ == "__main__":
    main()
