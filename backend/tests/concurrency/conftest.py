import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")
os.environ.setdefault("TEST_PROFILE", "race")

from app.auth.models import User
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.resources.models import Resource

get_settings.cache_clear()

# Real argon2 hash computed once at module import using required parameters
# (memory_cost 65536, time_cost 3, parallelism 2). Reused across seeded test users.
_REAL_PASSWORD_HASH = hash_password("valid-password-123")


def find_free_port(start_port: int = 18100, max_attempts: int = 100) -> int:
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"Could not allocate free port starting at {start_port}")


@pytest.fixture(scope="session")
def base_url() -> Generator[str, None, None]:
    external = os.environ.get("COMMONSBOOK_BASE_URL")
    if external:
        ready = False
        for _ in range(30):
            try:
                with urllib.request.urlopen(f"{external}/healthz", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.2)
        if not ready:
            raise RuntimeError(f"External COMMONSBOOK_BASE_URL {external} not ready on /healthz")
        yield external
        return

    # Self-sufficient fallback when running pytest directly without runner
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for concurrency tests")

    port = find_free_port(18200)
    url = f"http://127.0.0.1:{port}"
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    backend_python = sys.executable

    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "TEST_PROFILE": "race",
        "APP_ENV": "local",
        "JWT_SECRET": os.environ.get("JWT_SECRET")
        or "test-jwt-secret-minimum-32-bytes-long-12345678",
        "RATE_LIMIT_HMAC_SECRET": os.environ.get("RATE_LIMIT_HMAC_SECRET")
        or "test-hmac-secret-minimum-32-bytes-long-1234",
        "PYTHONPATH": str(repo_root / "backend"),
    }

    cmd = [
        backend_python,
        "-c",
        (
            "import sys; sys.path.insert(0, 'backend'); "
            "import uvicorn, app.db.session; "
            "from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker; "
            "from app.config import get_settings; "
            "url = get_settings().DATABASE_URL; "
            "settings = {'timezone': 'UTC', 'lock_timeout': '15s', "
            "'statement_timeout': '30s', 'idle_in_transaction_session_timeout': '30s'}; "
            "engine = create_async_engine("
            "    url, pool_size=20, max_overflow=0, pool_timeout=30.0, "
            "    connect_args={'server_settings': settings}"
            "); "
            "app.db.session._engine = engine; "
            "app.db.session._engine_url = url; "
            "app.db.session._sessionmaker = "
            "    async_sessionmaker(bind=engine, expire_on_commit=False); "
            f"uvicorn.run('app.main:app', host='127.0.0.1', port={port}, log_level='warning')"
        ),
    ]

    log_out = Path(__file__).parent / "uvicorn_stdout.log"
    log_err = Path(__file__).parent / "uvicorn_stderr.log"
    f_out = open(log_out, "w", encoding="utf-8")
    f_err = open(log_err, "w", encoding="utf-8")

    proc = subprocess.Popen(cmd, env=env, stdout=f_out, stderr=f_err, text=True)
    try:
        ready = False
        for _ in range(60):
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"{url}/healthz", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                continue

        if not ready:
            f_err.flush()
            err = log_err.read_text(encoding="utf-8")
            raise RuntimeError(f"Local Uvicorn server failed to become ready on {url}: {err}")

        yield url
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        f_out.close()
        f_err.close()
        try:
            if log_out.exists():
                log_out.unlink(missing_ok=True)
            if log_err.exists():
                log_err.unlink(missing_ok=True)
        except OSError:
            pass


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> Generator[None, None, None]:
    yield
    import app.db.session

    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None


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


async def create_users(count: int, prefix: str = "race") -> tuple[list[User], list[str]]:
    users = [
        User(
            id=uuid.uuid4(),
            email=f"{prefix}_{uuid.uuid4().hex[:8]}_{i}@example.com",
            password_hash=_REAL_PASSWORD_HASH,
            display_name=f"Race User {i}",
            role="member",
            enabled=True,
            version=1,
        )
        for i in range(count)
    ]
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            session.add_all(users)
    tokens = [make_token(u) for u in users]
    return users, tokens


async def create_resource(name_prefix: str = "RaceResource") -> Resource:
    r = Resource(
        id=uuid.uuid4(),
        name=f"{name_prefix}_{uuid.uuid4().hex[:8]}",
        description="Resource for race tests",
        location="Room 101",
        active=True,
        version=1,
    )
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            session.add(r)
    return r


def get_aligned_window(
    days_ahead: int = 1, hour: int = 10, duration_hours: int = 1
) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    target_date = (now + timedelta(days=days_ahead)).date()
    starts_at = datetime(
        target_date.year, target_date.month, target_date.day, hour, 0, 0, tzinfo=timezone.utc
    )
    ends_at = starts_at + timedelta(hours=duration_hours)
    return starts_at, ends_at
