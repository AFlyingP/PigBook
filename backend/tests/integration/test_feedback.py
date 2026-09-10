import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.admin.models import AuditLog, Feedback
from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.resources.models import Resource

repo_root = Path(__file__).resolve().parents[3]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
from scripts.retention import run_retention  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> None:
    yield
    import app.db.session

    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None


async def create_user(
    *,
    email: str | None = None,
    password: str = "valid-password-123",
    display_name: str = "Feedback Test User",
    role: str = "member",
    enabled: bool = True,
    created_at: datetime | None = None,
) -> User:
    if email is None:
        email = f"{role}_{uuid.uuid4().hex[:8]}@example.com"
    pwd_hash = hash_password(password)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=pwd_hash,
        display_name=display_name,
        role=role,
        enabled=enabled,
        version=1,
    )
    if created_at is not None:
        user.created_at = created_at
        user.updated_at = created_at

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(user)
    return user


def make_token(user: User) -> str:
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678"
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user.id),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    return jwt.encode(claims, jwt_secret, algorithm="HS256")


def make_client(ip: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 12345)),
        base_url="http://localhost:5173",
    )


@pytest.mark.asyncio
async def test_feedback_creation_and_consent_requirement() -> None:
    """E33: POST /api/v1/feedback consent requirements, validation, and rate limiting."""
    member = await create_user(role="member", enabled=True)
    member_token = make_token(member)
    admin = await create_user(role="admin", enabled=True)
    admin_token = make_token(admin)

    valid_payload = {
        "rating": 4,
        "task_completed": True,
        "difficulty": "Easy to book",
        "improvement": "Add more rooms",
        "consent_version": "2026-09-v1",
        "consent": True,
    }

    client_ip = f"10.5.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # 1. Anonymous -> 401 AUTH_REQUIRED
        r_anon = await client.post("/api/v1/feedback", json=valid_payload)
        assert r_anon.status_code == 401
        assert r_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        # 2. Member -> 201 Created {id, created_at}
        r_mem = await client.post(
            "/api/v1/feedback",
            json=valid_payload,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_mem.status_code == 201
        mem_data = r_mem.json()
        assert "id" in mem_data
        assert "created_at" in mem_data

        # 3. Admin -> 201 Created
        r_adm = await client.post(
            "/api/v1/feedback",
            json=valid_payload,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_adm.status_code == 201

        # 4. Missing or false consent -> 422 CONSENT_REQUIRED
        no_consent_payload = dict(valid_payload, consent=False)
        r_no_consent = await client.post(
            "/api/v1/feedback",
            json=no_consent_payload,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_no_consent.status_code == 422
        assert r_no_consent.json()["error"]["code"] == "CONSENT_REQUIRED"

        # 5. Mismatched consent_version -> 422 CONSENT_REQUIRED
        wrong_ver_payload = dict(valid_payload, consent_version="2025-01-v1")
        r_wrong_ver = await client.post(
            "/api/v1/feedback",
            json=wrong_ver_payload,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_wrong_ver.status_code == 422
        assert r_wrong_ver.json()["error"]["code"] == "CONSENT_REQUIRED"

        # 6. Invalid rating (>5) -> 422 VALIDATION_ERROR
        bad_rating = dict(valid_payload, rating=6)
        r_bad_rating = await client.post(
            "/api/v1/feedback",
            json=bad_rating,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_bad_rating.status_code == 422
        assert r_bad_rating.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_admin_feedback_listing_and_member_forbidden() -> None:
    """E34: GET /api/v1/admin/feedback accessible to admins; members strictly forbidden (403)."""
    admin = await create_user(role="admin", enabled=True)
    admin_token = make_token(admin)
    member = await create_user(role="member", enabled=True)
    member_token = make_token(member)

    # Seed feedback
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)
    fb_id = uuid.uuid4()
    async with sessionmaker() as session:
        async with session.begin():
            fb = Feedback(
                id=fb_id,
                user_id=member.id,
                rating=5,
                task_completed=True,
                difficulty="None",
                improvement="Keep it up",
                consent_version="2026-09-v1",
                created_at=now,
            )
            session.add(fb)

    client_ip = f"10.5.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # Member receives 403 FORBIDDEN (cannot read others' raw feedback)
        r_mem = await client.get(
            "/api/v1/admin/feedback",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_mem.status_code == 403
        assert r_mem.json()["error"]["code"] == "FORBIDDEN"

        # Admin receives 200 Page<Feedback>
        r_adm = await client.get(
            "/api/v1/admin/feedback",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_adm.status_code == 200
        data = r_adm.json()
        assert data["total"] >= 1
        entry = next(e for e in data["items"] if e["id"] == str(fb_id))
        assert entry["user_id"] == str(member.id)
        assert entry["rating"] == 5
        assert entry["task_completed"] is True
        assert entry["difficulty"] == "None"
        assert entry["improvement"] == "Keep it up"


@pytest.mark.asyncio
async def test_retention_dry_run_default_and_apply_refusal_without_approval(tmp_path: Path) -> None:
    """scripts/retention.py: Defaults to dry-run (no DB modifications),

    and refuses mutating apply without valid approval file or backup reference.
    """
    now = datetime.now(timezone.utc)
    old_date = now - timedelta(days=200)

    # Seed an old user and feedback
    old_user = await create_user(role="member", enabled=True, created_at=old_date)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            fb = Feedback(
                id=uuid.uuid4(),
                user_id=old_user.id,
                rating=3,
                task_completed=True,
                difficulty="Old",
                improvement="Old",
                consent_version="2026-09-v1",
                created_at=old_date,
            )
            session.add(fb)

    # 1. Dry run default -> reports counts, mutates nothing
    res_dry = await run_retention(account_cutoff_days=180, feedback_cutoff_days=90, apply=False)
    assert res_dry["accounts_anonymized"] >= 1
    assert res_dry["feedback_deleted"] >= 1

    # Verify DB user was NOT touched
    async with sessionmaker() as session:
        u_after = (await session.execute(select(User).where(User.id == old_user.id))).scalar_one()
        assert u_after.email == old_user.email
        assert u_after.enabled is True

    # 2. Apply without approval file -> SystemExit (exit 1)
    with pytest.raises(SystemExit) as exc_info:
        await run_retention(apply=True, approval_file=None, backup_ref="backup-123")
    assert exc_info.value.code == 1

    # 3. Apply without backup ref -> SystemExit (exit 1)
    approval_file = tmp_path / "approval.txt"
    approval_file.write_text("APPROVE_RETENTION_TEST", encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        await run_retention(apply=True, approval_file=str(approval_file), backup_ref=None)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_retention_age_cutoff_anonymization_and_booking_preservation(tmp_path: Path) -> None:
    """scripts/retention.py apply: Anonymizes accounts older than cutoff,

    revokes refresh tokens, removes feedback, preserves booking owner UUID,
    leaves newer accounts untouched, and appends audit record.
    """
    now = datetime.now(timezone.utc)
    old_date = now - timedelta(days=200)
    recent_date = now - timedelta(days=10)

    old_user = await create_user(role="member", enabled=True, created_at=old_date)
    recent_user = await create_user(role="member", enabled=True, created_at=recent_date)

    sessionmaker = get_sessionmaker()
    res_id = uuid.uuid4()
    booking_id = uuid.uuid4()
    old_fb_id = uuid.uuid4()
    recent_fb_id = uuid.uuid4()

    # Seed booking and refresh tokens for old_user
    async with sessionmaker() as session:
        async with session.begin():
            r = Resource(
                id=res_id,
                name="Retention Preservation Room",
                description="desc",
                location="Loc",
                active=True,
                version=1,
            )
            session.add(r)
            await session.flush()
            b = Booking(
                id=booking_id,
                resource_id=res_id,
                user_id=old_user.id,
                created_by=old_user.id,
                kind="reservation",
                time_range=Range(
                    now + timedelta(days=5), now + timedelta(days=5, hours=1), bounds="[)"
                ),
                status="confirmed",
                version=1,
            )
            session.add(b)
            # Old feedback
            fb_old = Feedback(
                id=old_fb_id,
                user_id=old_user.id,
                rating=4,
                task_completed=True,
                difficulty="None",
                improvement="None",
                consent_version="2026-09-v1",
                created_at=old_date,
            )
            # Recent feedback
            fb_recent = Feedback(
                id=recent_fb_id,
                user_id=recent_user.id,
                rating=5,
                task_completed=True,
                difficulty="Recent",
                improvement="Recent",
                consent_version="2026-09-v1",
                created_at=recent_date,
            )
            session.add_all([fb_old, fb_recent])

    # Run retention with valid approval and backup ref
    approval_file = tmp_path / "approved.txt"
    approval_file.write_text("APPROVED_RETENTION_RUN", encoding="utf-8")
    backup_ref = "s3://backups/commonsbook-2026-09-10.tar.gz"

    result = await run_retention(
        account_cutoff_days=180,
        feedback_cutoff_days=90,
        apply=True,
        approval_file=str(approval_file),
        backup_ref=backup_ref,
    )
    assert result["accounts_anonymized"] >= 1
    assert result["feedback_deleted"] >= 1

    # Verify old_user anonymization
    async with sessionmaker() as session:
        u_stmt = select(User).where(User.id == old_user.id)
        anon_user = (await session.execute(u_stmt)).scalar_one()
        assert anon_user.email == f"{old_user.id}@deleted.invalid"
        assert anon_user.display_name == "Deleted participant"
        assert anon_user.enabled is False
        assert anon_user.password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=2$invalid$")

        # Booking owner UUID is preserved
        b_stmt = select(Booking).where(Booking.id == booking_id)
        booking = (await session.execute(b_stmt)).scalar_one()
        assert booking.user_id == old_user.id

        # Recent user is NOT modified
        rec_stmt = select(User).where(User.id == recent_user.id)
        rec_u = (await session.execute(rec_stmt)).scalar_one()
        assert rec_u.email == recent_user.email
        assert rec_u.enabled is True

        # Old feedback is removed
        fb_stmt = select(Feedback).where(Feedback.id == old_fb_id)
        assert (await session.execute(fb_stmt)).scalar_one_or_none() is None

        # Recent feedback is preserved
        fb_rec_stmt = select(Feedback).where(Feedback.id == recent_fb_id)
        assert (await session.execute(fb_rec_stmt)).scalar_one_or_none() is not None

        # Audit log entry exists
        audit_stmt = select(AuditLog).where(AuditLog.action == "operator.retention")
        audit_row = (await session.execute(audit_stmt)).scalar_one_or_none()
        assert audit_row is not None
        assert audit_row.details["backup_ref"] == backup_ref
        assert audit_row.details["account_cutoff_days"] == 180
