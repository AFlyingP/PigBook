#!/usr/bin/env python3
import argparse
import atexit
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Add scripts directory to sys.path for verification_cache import
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from verification_cache import (  # noqa: E402
    check_reuse_eligibility,
    compute_fingerprint,
    create_reused_manifest,
    get_reusable_gates,
    is_evidence_reuse_activated,
)

LEGAL_DOCUMENT_CHECKS = {"traceability", "evidence", "demo", "feedback-audit"}
RECOGNIZED_TARGETS = {
    "bootstrap",
    "lint",
    "unit",
    "integration",
    "concurrency",
    "permissions",
    "regression",
    "ticket",
    "build",
    "smoke",
    "race-lab",
    "load",
    "rollback",
    "traceability",
    "evidence",
    "demo",
    "feedback-audit",
    "frontend",
}


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    return name


def check_no_skips(stdout: str, tool: str) -> str | None:
    """
    Enforce no-skips policy.
    Treats zero collected tests or any skipped tests as a failure.
    """
    if tool == "pytest":
        # Check for skipped tests
        skip_match = re.search(r"\b(\d+)\s+skipped\b", stdout)
        if skip_match and int(skip_match.group(1)) > 0:
            return f"Policy violation: {skip_match.group(1)} test(s) skipped in pytest"

        # Check for zero collected tests
        if "collected 0 items" in stdout or "no tests ran" in stdout:
            return "Policy violation: 0 tests collected in pytest execution"

    elif tool == "vitest":
        skip_match = re.search(r"\b(\d+)\s+skipped\b", stdout)
        if skip_match and int(skip_match.group(1)) > 0:
            return f"Policy violation: {skip_match.group(1)} test(s) skipped in vitest"

        if (
            "Tests  0 passed" in stdout
            or "No test files found" in stdout
            or "collected 0 items" in stdout
        ):
            return "Policy violation: 0 tests collected in vitest execution"

    return None


def sanitize_command(cmd_args: list[str]) -> list[str]:
    """Redact raw approval tokens and mask credentials in URLs for manifests, logs, and stdout."""
    sanitized: list[str] = []
    redact_next = False
    for arg in cmd_args:
        if redact_next:
            sanitized.append("[REDACTED]")
            redact_next = False
            continue
        if arg == "--approval-token":
            sanitized.append(arg)
            redact_next = True
            continue
        if arg.startswith("--approval-token="):
            sanitized.append("--approval-token=[REDACTED]")
            continue
        # Mask credentials in URLs: postgresql://user:pass@host:port/db
        masked = re.sub(r"://([^:]+):([^@]+)@", r"://\g<1>:***@", arg)
        sanitized.append(masked)
    if redact_next:
        sanitized.append("[REDACTED]")
    return sanitized


def sanitize_text(text: str, secrets: list[str] | None = None) -> str:
    """Mask credentials in URLs and redact secret tokens/passwords."""
    if not text:
        return text
    masked = re.sub(r"://([^:\s\'\"]+):([^@\s\'\"]+)@", r"://\g<1>:***@", text)
    if secrets:
        for s in secrets:
            if s and len(s) >= 4:
                masked = masked.replace(s, "[REDACTED]")
    return masked


def run_command(
    cmd: list[str],
    evidence_dir: Path,
    cmd_index: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check_tool: str | None = None,
) -> int:
    display_cwd = str(cwd) if cwd else str(REPO_ROOT)
    sanitized_display = " ".join(sanitize_command(cmd))
    print(f"[{cmd_index}] Running: {sanitized_display} (cwd: {display_cwd})")

    # Collect any secrets passed via env or arguments for output sanitization
    secrets_to_redact: list[str] = []
    if env:
        for k, v in env.items():
            k_upper = k.upper()
            if any(term in k_upper for term in ("TOKEN", "PASSWORD", "SECRET", "DSN", "URL")):
                for match in re.finditer(r"://[^:\s\'\"]+:([^@\s\'\"]+)@", v):
                    secrets_to_redact.append(match.group(1))
                if any(term in k_upper for term in ("TOKEN", "PASSWORD", "SECRET")):
                    secrets_to_redact.append(v)

    stdout_text = ""
    stderr_text = ""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdout_text = sanitize_text(proc.stdout, secrets_to_redact)
        stderr_text = sanitize_text(proc.stderr, secrets_to_redact)
        return_code = proc.returncode
    except Exception as exc:
        # Use generic failure messages to prevent exception messages from leaking secrets
        err_msg = f"Command execution failed: {type(exc).__name__}\n"
        print(f"[{cmd_index}] {err_msg.strip()}", file=sys.stderr)
        stderr_text = err_msg
        return_code = 1

    if stdout_text:
        print(stdout_text, end="")
    if stderr_text:
        print(stderr_text, end="", file=sys.stderr)

    # If the process exited 0, check for skips or zero collected tests
    if return_code == 0 and check_tool:
        skip_err = check_no_skips(stdout_text, check_tool)
        if skip_err:
            print(f"\nError: {skip_err}", file=sys.stderr)
            return_code = 1

    stdout_file = evidence_dir / f"cmd_{cmd_index:02d}_stdout.txt"
    stderr_file = evidence_dir / f"cmd_{cmd_index:02d}_stderr.txt"

    stdout_file.write_text(stdout_text, encoding="utf-8")
    stderr_file.write_text(stderr_text, encoding="utf-8")

    return return_code


def get_git_sha() -> str:
    git_bin = find_tool("git")
    proc = subprocess.run(
        [git_bin, "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode == 0:
        return proc.stdout.strip()
    return "no-commits-yet"


def compute_configuration_hash() -> str:
    hasher = hashlib.sha256()
    config_paths = [
        REPO_ROOT / "backend" / "pyproject.toml",
        REPO_ROOT / "backend" / "uv.lock",
        REPO_ROOT / "frontend" / "package.json",
        REPO_ROOT / "frontend" / "package-lock.json",
        REPO_ROOT / "scripts" / "verification.json",
    ]
    for p in config_paths:
        if p.exists():
            hasher.update(p.read_bytes())
    frag_dir = REPO_ROOT / "scripts" / "verification.d"
    if frag_dir.exists():
        for frag in sorted(frag_dir.glob("*.json")):
            hasher.update(frag.read_bytes())
    return hasher.hexdigest()


def get_tool_versions() -> dict[str, str]:
    versions: dict[str, str] = {
        "python_launcher": sys.version.split()[0],
        "platform": platform.platform(),
    }
    uv_bin = find_tool("uv")
    try:
        proc = subprocess.run([uv_bin, "--version"], capture_output=True, text=True)
        versions["uv"] = proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        versions["uv"] = "not-found"

    node_bin = find_tool("node")
    try:
        proc = subprocess.run([node_bin, "-v"], capture_output=True, text=True)
        versions["node"] = proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        versions["node"] = "not-found"

    npm_bin = find_tool("npm")
    try:
        proc = subprocess.run([npm_bin, "-v"], capture_output=True, text=True)
        versions["npm"] = proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        versions["npm"] = "not-found"

    backend_python = (
        REPO_ROOT
        / "backend"
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    probes = {
        "frontend_packages": [npm_bin, "--prefix", "frontend", "ls", "--all", "--json"],
        "backend_runtime": [
            str(backend_python),
            "-c",
            "import sys,importlib.metadata as m; print(sys.version); "
            "print(sorted((d.metadata['Name'],d.version) for d in m.distributions()))",
        ],
        "docker": [find_tool("docker"), "--version"],
        "compose": [find_tool("docker"), "compose", "version"],
        "postgres_image": [
            find_tool("docker"),
            "image",
            "inspect",
            "postgres:16.15",
            "--format",
            "{{.Id}}",
        ],
    }
    for name, cmd in probes.items():
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            versions[name] = proc.stdout.strip() if proc.returncode == 0 else "unknown"
            if name == "frontend_packages" and proc.returncode == 0:
                versions[name] = hashlib.sha256(proc.stdout.encode()).hexdigest()
        except (OSError, subprocess.TimeoutExpired):
            versions[name] = "not-found"
    # Record hashes, never environment values which may contain credentials.
    relevant_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "EVIDENCE_DIR",
            "EVIDENCE_ROOT",
            "TEST_RUN_ID",
            "PWD",
            "OLDPWD",
            "SHLVL",
            "_",
        }
    }
    versions["environment_sha256"] = hashlib.sha256(
        json.dumps(relevant_env, sort_keys=True).encode()
    ).hexdigest()
    return versions


def find_free_port(start_port: int, max_attempts: int = 100) -> int:
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"Could not allocate free port starting at {start_port}")


class IsolatedDatabaseManager:
    """Manages ephemeral PostgreSQL 16 database lifecycle strictly scoped by TEST_RUN_ID."""

    _active_managers: set["IsolatedDatabaseManager"] = set()
    _atexit_registered: bool = False

    def __init__(self, evidence_dir: Path, run_id: str) -> None:
        self.evidence_dir = evidence_dir
        self.test_run_id = f"{run_id}_{uuid.uuid4().hex}"
        # Derive deterministic, sanitized project and database identifiers
        slug = hashlib.sha256(self.test_run_id.encode("utf-8")).hexdigest()[:12]
        self.project_name = f"cb_test_{slug}"
        self.db_name = f"cb_test_{slug}"
        self.db_user = "commonsbook_test"
        self.db_password = "commonsbook_test"
        self._started = False
        self._stopped = False

        port_offset = (
            int(
                hashlib.sha256(f"port_{self.test_run_id}".encode("utf-8")).hexdigest(),
                16,
            )
            % 2000
        )
        base_port = 15432 + port_offset
        self.allocated_port = find_free_port(base_port)
        self.docker_bin = find_tool("docker")

    @classmethod
    def _ensure_atexit_registered(cls) -> None:
        if not cls._atexit_registered:
            cls._atexit_registered = True
            atexit.register(cls._cleanup_all)

    @classmethod
    def _cleanup_all(cls) -> None:
        for mgr in list(cls._active_managers):
            try:
                mgr.stop()
            except Exception:
                pass

    def __enter__(self) -> "IsolatedDatabaseManager":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def __del__(self) -> None:
        if getattr(self, "_started", False) and not getattr(self, "_stopped", False):
            try:
                self.stop()
            except Exception:
                pass

    def start(self, command_log: list[dict[str, Any]]) -> str:
        self._started = True
        IsolatedDatabaseManager._active_managers.add(self)
        IsolatedDatabaseManager._ensure_atexit_registered()

        override_yaml = f'services:\n  db:\n    ports:\n      - "127.0.0.1:{self.allocated_port}:5432"\n'
        cmd = [
            self.docker_bin,
            "compose",
            "-p",
            self.project_name,
            "-f",
            "compose.test.yaml",
            "-f",
            "-",
            "up",
            "-d",
            "--wait",
            "db",
        ]
        print(
            f"Starting isolated test database in namespace '{self.project_name}' "
            f"on port {self.allocated_port}..."
        )
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            input=override_yaml,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "POSTGRES_DB": self.db_name,
                "POSTGRES_USER": self.db_user,
                "POSTGRES_PASSWORD": self.db_password,
            },
        )
        command_log.append(
            {
                "cmd": sanitize_command(
                    cmd + [f"(stdin: port 127.0.0.1:{self.allocated_port}:5432)"]
                ),
                "exit_code": proc.returncode,
            }
        )
        index = len(command_log)
        if self.evidence_dir.exists():
            (self.evidence_dir / f"cmd_{index:02d}_stdout.txt").write_text(
                proc.stdout, encoding="utf-8"
            )
            (self.evidence_dir / f"cmd_{index:02d}_stderr.txt").write_text(
                proc.stderr, encoding="utf-8"
            )
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            raise RuntimeError(
                f"Failed to start isolated database container: {proc.stderr}"
            )

        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@127.0.0.1:{self.allocated_port}/{self.db_name}"

    def stop(self, command_log: list[dict[str, Any]] | None = None) -> None:
        if self._stopped:
            return

        cmd = [
            self.docker_bin,
            "compose",
            "-p",
            self.project_name,
            "-f",
            "compose.test.yaml",
            "down",
            "-v",
            "--remove-orphans",
        ]
        print(
            f"Tearing down isolated test database in namespace '{self.project_name}'..."
        )
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if command_log is not None:
            command_log.append(
                {"cmd": sanitize_command(cmd), "exit_code": proc.returncode}
            )
            index = len(command_log)
            if self.evidence_dir.exists():
                (self.evidence_dir / f"cmd_{index:02d}_stdout.txt").write_text(
                    proc.stdout, encoding="utf-8"
                )
                (self.evidence_dir / f"cmd_{index:02d}_stderr.txt").write_text(
                    proc.stderr, encoding="utf-8"
                )

        # Genuinely ensure all runner-owned containers for this project are removed
        self._force_cleanup()

        if proc.returncode != 0:
            raise RuntimeError(f"Database cleanup failed for {self.project_name}")

        self._stopped = True
        IsolatedDatabaseManager._active_managers.discard(self)

    def _force_cleanup(self) -> None:
        """Ensure no containers or networks remain for this project."""
        try:
            # Check for leftover containers by project label
            ps_cmd = [
                self.docker_bin,
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self.project_name}",
            ]
            proc = subprocess.run(ps_cmd, capture_output=True, text=True)
            cids = [c.strip() for c in proc.stdout.splitlines() if c.strip()]
            if cids:
                subprocess.run(
                    [self.docker_bin, "rm", "-f"] + cids,
                    capture_output=True,
                )

            # Check for leftover containers by name pattern
            ps_name_cmd = [
                self.docker_bin,
                "ps",
                "-a",
                "-q",
                "--filter",
                f"name={self.project_name}",
            ]
            proc_name = subprocess.run(ps_name_cmd, capture_output=True, text=True)
            cids_name = [c.strip() for c in proc_name.stdout.splitlines() if c.strip()]
            if cids_name:
                subprocess.run(
                    [self.docker_bin, "rm", "-f"] + cids_name,
                    capture_output=True,
                )

            # Check for leftover networks
            net_cmd = [
                self.docker_bin,
                "network",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self.project_name}",
            ]
            proc_net = subprocess.run(net_cmd, capture_output=True, text=True)
            nids = [n.strip() for n in proc_net.stdout.splitlines() if n.strip()]
            if nids:
                subprocess.run(
                    [self.docker_bin, "network", "rm"] + nids,
                    capture_output=True,
                )
        except Exception:
            pass


def load_manifests(
    manifest_path: Path | None = None,
    fragments_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_file = manifest_path or (REPO_ROOT / "scripts" / "verification.json")
    if not manifest_file.exists():
        sys.exit("Error: scripts/verification.json not found")

    central_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

    frag_dir = fragments_dir or (REPO_ROOT / "scripts" / "verification.d")
    fragments: dict[str, dict[str, Any]] = {}

    if frag_dir.exists():
        for frag_path in sorted(frag_dir.glob("*.json")):
            data = json.loads(frag_path.read_text(encoding="utf-8"))

            required_keys = {
                "ticket_id",
                "pytest_paths",
                "vitest_paths",
                "playwright_paths",
                "document_checks",
            }
            if set(data.keys()) != required_keys:
                sys.exit(
                    f"Error: Fragment {frag_path.name} must have exactly keys: {required_keys}"
                )

            ticket_id = data["ticket_id"]
            if ticket_id in fragments:
                sys.exit(f"Error: Duplicate identifier found: {ticket_id}")

            pytest_paths = data["pytest_paths"]
            vitest_paths = data["vitest_paths"]
            playwright_paths = data["playwright_paths"]
            doc_checks = data["document_checks"]

            if (
                not isinstance(pytest_paths, list)
                or not isinstance(vitest_paths, list)
                or not isinstance(playwright_paths, list)
                or not isinstance(doc_checks, list)
            ):
                sys.exit(f"Error: Fragment {frag_path.name} path lists must be arrays")

            total_checks = (
                len(pytest_paths)
                + len(vitest_paths)
                + len(playwright_paths)
                + len(doc_checks)
            )
            if total_checks == 0:
                sys.exit(
                    f"Error: Fragment {frag_path.name} must declare at least one check"
                )

            for dc in doc_checks:
                if dc not in LEGAL_DOCUMENT_CHECKS:
                    sys.exit(
                        f"Error: Illegal document check '{dc}' in {frag_path.name}"
                    )

            for p in pytest_paths:
                if p.endswith(".md"):
                    sys.exit(f"Error: Markdown path '{p}' forbidden in pytest_paths")
                if not (REPO_ROOT / p).exists():
                    sys.exit(f"Error: Declared pytest path '{p}' does not exist")

            for p in vitest_paths:
                if not (REPO_ROOT / p).exists():
                    sys.exit(f"Error: Declared vitest path '{p}' does not exist")

            for p in playwright_paths:
                if not (REPO_ROOT / p).exists():
                    sys.exit(f"Error: Declared playwright path '{p}' does not exist")

            fragments[ticket_id] = data

    return central_manifest, fragments


def run_target(
    target: str,
    ticket_id: str | None,
    central_manifest: dict[str, Any],
    fragments: dict[str, dict[str, Any]],
    evidence_dir: Path,
    command_log: list[dict[str, Any]],
    run_id: str,
) -> int:
    uv_bin = find_tool("uv")
    npm_bin = find_tool("npm")

    if target == "bootstrap":
        if shutil.which("uv") is None:
            tooling = central_manifest.get("tooling", {})
            pinned_uv = tooling.get("uv_version", "0.12.10")
            print(f"uv not found; installing uv=={pinned_uv}...")
            install_cmd = [sys.executable, "-m", "pip", "install", f"uv=={pinned_uv}"]
            code = run_command(install_cmd, evidence_dir, len(command_log) + 1)
            command_log.append({"cmd": install_cmd, "exit_code": code})
            if code != 0:
                return code
            uv_bin = find_tool("uv")

        sync_cmd = [
            uv_bin,
            "sync",
            "--frozen",
            "--python",
            "3.12",
            "--project",
            "backend",
        ]
        code = run_command(sync_cmd, evidence_dir, len(command_log) + 1)
        command_log.append({"cmd": sync_cmd, "exit_code": code})
        if code != 0:
            return code

        npm_cmd = [npm_bin, "ci", "--prefix", "frontend"]
        code = run_command(npm_cmd, evidence_dir, len(command_log) + 1)
        command_log.append({"cmd": npm_cmd, "exit_code": code})
        return code

    elif target == "lint":
        lint_commands = [
            [uv_bin, "run", "--project", "backend", "ruff", "check", "backend"],
            [
                uv_bin,
                "run",
                "--project",
                "backend",
                "ruff",
                "format",
                "--check",
                "backend",
            ],
            [uv_bin, "run", "--project", "backend", "mypy", "backend/app"],
            [npm_bin, "--prefix", "frontend", "run", "lint"],
            [npm_bin, "--prefix", "frontend", "run", "typecheck"],
        ]
        for cmd in lint_commands:
            code = run_command(cmd, evidence_dir, len(command_log) + 1)
            command_log.append({"cmd": cmd, "exit_code": code})
            if code != 0:
                return code
        return 0

    elif target == "unit":
        code = run_command(
            [uv_bin, "run", "--project", "backend", "pytest", "backend/tests/unit"],
            evidence_dir,
            len(command_log) + 1,
            check_tool="pytest",
        )
        command_log.append({"cmd": ["pytest", "backend/tests/unit"], "exit_code": code})
        if code != 0:
            return code

        code = run_command(
            [npm_bin, "--prefix", "frontend", "test", "--", "--run"],
            evidence_dir,
            len(command_log) + 1,
            check_tool="vitest",
        )
        command_log.append({"cmd": ["vitest", "--run"], "exit_code": code})
        return code

    elif target == "frontend":
        for script in ("lint", "typecheck", "test", "build"):
            cmd = [npm_bin, "--prefix", "frontend", "run", script]
            if script == "test":
                cmd += ["--", "--run"]
            code = run_command(
                cmd,
                evidence_dir,
                len(command_log) + 1,
                check_tool="vitest" if script == "test" else None,
            )
            command_log.append({"cmd": cmd, "exit_code": code})
            if code:
                return code
        return 0

    elif target == "integration":
        db_mgr = IsolatedDatabaseManager(evidence_dir=evidence_dir, run_id=run_id)
        try:
            database_url = db_mgr.start(command_log)
            test_env = {
                **os.environ,
                "DATABASE_URL": database_url,
                "TEST_RUN_ID": db_mgr.test_run_id,
            }
            cmd = [
                uv_bin,
                "run",
                "--project",
                "backend",
                "pytest",
                "backend/tests/integration",
            ]
            code = run_command(
                cmd,
                evidence_dir,
                len(command_log) + 1,
                env=test_env,
                check_tool="pytest",
            )
            command_log.append({"cmd": cmd, "exit_code": code})
            return code
        finally:
            db_mgr.stop(command_log)

    elif target == "concurrency":
        db_mgr = IsolatedDatabaseManager(evidence_dir=evidence_dir, run_id=run_id)
        server_proc = None
        server_stdout_f = None
        server_stderr_f = None
        try:
            database_url = db_mgr.start(command_log)
            # Run alembic upgrade head
            upgrade_cmd = [
                uv_bin,
                "run",
                "--project",
                "backend",
                "alembic",
                "-c",
                "backend/alembic.ini",
                "upgrade",
                "head",
            ]
            env_db = {
                **os.environ,
                "DATABASE_URL": database_url,
                "TEST_RUN_ID": db_mgr.test_run_id,
            }
            code = run_command(
                upgrade_cmd, evidence_dir, len(command_log) + 1, env=env_db
            )
            command_log.append({"cmd": upgrade_cmd, "exit_code": code})
            if code != 0:
                return code

            server_port = find_free_port(18000)
            server_url = f"http://127.0.0.1:{server_port}"

            server_env = {
                **os.environ,
                "DATABASE_URL": database_url,
                "TEST_RUN_ID": db_mgr.test_run_id,
                "TEST_PROFILE": "race",
                "APP_ENV": "local",
                "JWT_SECRET": os.environ.get("JWT_SECRET")
                or "test-jwt-secret-minimum-32-bytes-long-12345678",
                "RATE_LIMIT_HMAC_SECRET": os.environ.get("RATE_LIMIT_HMAC_SECRET")
                or "test-hmac-secret-minimum-32-bytes-long-1234",
                "PYTHONPATH": str(REPO_ROOT / "backend"),
            }
            backend_python = (
                REPO_ROOT
                / "backend"
                / ".venv"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            server_cmd = [
                str(backend_python),
                "-c",
                (
                    "import sys; sys.path.insert(0, 'backend'); "
                    "import uvicorn, app.db.session; "
                    "from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker; "
                    "from app.config import get_settings; "
                    "url = get_settings().DATABASE_URL; "
                    "engine = create_async_engine("
                    "    url, pool_size=20, max_overflow=0, pool_timeout=30.0, "
                    "    connect_args={'server_settings': {'timezone': 'UTC', 'lock_timeout': '15s', 'statement_timeout': '30s', 'idle_in_transaction_session_timeout': '30s'}}"
                    "); "
                    "app.db.session._engine = engine; "
                    "app.db.session._engine_url = url; "
                    "app.db.session._sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False); "
                    f"uvicorn.run('app.main:app', host='127.0.0.1', port={server_port}, log_level='warning')"
                ),
            ]
            print(
                f"Starting Uvicorn server on port {server_port} for concurrency gate..."
            )
            server_stdout_f = open(
                evidence_dir / "uvicorn_stdout.txt", "w", encoding="utf-8"
            )
            server_stderr_f = open(
                evidence_dir / "uvicorn_stderr.txt", "w", encoding="utf-8"
            )
            server_proc = subprocess.Popen(
                server_cmd,
                cwd=REPO_ROOT,
                env=server_env,
                stdout=server_stdout_f,
                stderr=server_stderr_f,
                text=True,
            )
            # Poll /healthz until ready
            ready = False
            for _ in range(60):
                time.sleep(0.5)
                if server_proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(
                        f"{server_url}/healthz", timeout=1
                    ) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    continue

            if not ready:
                server_stdout_f.flush()
                server_stderr_f.flush()
                err_content = (evidence_dir / "uvicorn_stderr.txt").read_text(
                    encoding="utf-8"
                )
                out_content = (evidence_dir / "uvicorn_stdout.txt").read_text(
                    encoding="utf-8"
                )
                raise RuntimeError(
                    f"Uvicorn server failed to become ready on {server_url}:\n{err_content}\n{out_content}"
                )

            # Run pytest backend/tests/concurrency
            test_env = {
                **server_env,
                "COMMONSBOOK_BASE_URL": server_url,
                "EVIDENCE_DIR": str(evidence_dir),
            }
            cmd = [
                uv_bin,
                "run",
                "--project",
                "backend",
                "pytest",
                "backend/tests/concurrency",
            ]
            code = run_command(
                cmd,
                evidence_dir,
                len(command_log) + 1,
                env=test_env,
                check_tool="pytest",
            )
            command_log.append({"cmd": cmd, "exit_code": code})
            return code
        finally:
            if server_proc and server_proc.poll() is None:
                server_proc.terminate()
                try:
                    server_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server_proc.kill()
                    server_proc.wait(timeout=2)
            if server_stdout_f:
                server_stdout_f.close()
            if server_stderr_f:
                server_stderr_f.close()
            db_mgr.stop(command_log)

    elif target == "permissions":
        db_mgr = IsolatedDatabaseManager(evidence_dir=evidence_dir, run_id=run_id)
        try:
            database_url = db_mgr.start(command_log)
            test_env = {
                **os.environ,
                "DATABASE_URL": database_url,
                "TEST_RUN_ID": db_mgr.test_run_id,
                "JWT_SECRET": os.environ.get("JWT_SECRET")
                or "test-jwt-secret-minimum-32-bytes-long-12345678",
                "RATE_LIMIT_HMAC_SECRET": os.environ.get("RATE_LIMIT_HMAC_SECRET")
                or "test-hmac-secret-minimum-32-bytes-long-1234",
            }
            cmd = [
                uv_bin,
                "run",
                "--project",
                "backend",
                "pytest",
                "backend/tests/integration/test_permissions.py",
            ]
            code = run_command(
                cmd,
                evidence_dir,
                len(command_log) + 1,
                env=test_env,
                check_tool="pytest",
            )
            command_log.append({"cmd": cmd, "exit_code": code})
            if code != 0:
                return code

            backend_python = (
                REPO_ROOT
                / "backend"
                / ".venv"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            check_matrix_cmd = [
                str(backend_python),
                "-c",
                (
                    "import re, sys; "
                    "sys.path.insert(0, 'backend'); "
                    "from app.auth.dependencies import policy_registry; "
                    "content = open('backend/tests/integration/test_permissions.py', encoding='utf-8').read(); "
                    "tested_eids = set(re.findall(r'# \\d+\\.\\s+(E\\d+):', content)); "
                    "missing = set(policy_registry.keys()) - tested_eids; "
                    "assert not missing, f'Policy registry endpoints missing test matrix coverage: {missing}'; "
                    "print(f'All {len(policy_registry)} endpoints in policy_registry verified in permissions matrix: {sorted(policy_registry.keys())}')"
                ),
            ]
            code = run_command(
                check_matrix_cmd,
                evidence_dir,
                len(command_log) + 1,
                env=test_env,
            )
            command_log.append({"cmd": check_matrix_cmd, "exit_code": code})
            return code
        finally:
            db_mgr.stop(command_log)

    elif target == "race-lab":
        db_mgr = IsolatedDatabaseManager(evidence_dir=evidence_dir, run_id=run_id)
        try:
            database_url = db_mgr.start(command_log)
            lab_run_id = f"gate_{uuid.uuid4().hex[:12]}"
            lab_db_name = f"commonsbook_racelab_{lab_run_id}"
            approval_token = uuid.uuid4().hex + uuid.uuid4().hex
            token_sha256 = hashlib.sha256(approval_token.encode("utf-8")).hexdigest()

            parsed_db = urllib.parse.urlsplit(database_url)

            # Persist ONLY a redacted/hash-only approval record in final evidence
            # (never the raw token)
            evidence_approval_file = evidence_dir / "approval.json"
            evidence_approval_data = {
                "run_id": lab_run_id,
                "database": lab_db_name,
                "host": parsed_db.hostname or "127.0.0.1",
                "port": parsed_db.port or 5432,
                "token_sha256": token_sha256,
                "actions": ["create", "cleanup"],
            }
            evidence_approval_file.write_text(
                json.dumps(evidence_approval_data, indent=2), encoding="utf-8"
            )

            admin_url = urllib.parse.urlunsplit(parsed_db._replace(path="/postgres"))
            backend_python = (
                REPO_ROOT
                / "backend"
                / ".venv"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )

            # Race demo receives actual approval material via temporary directory OUTSIDE evidence
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_approval_file = Path(temp_dir) / "approval.json"
                temp_approval_data = {
                    "run_id": lab_run_id,
                    "database": lab_db_name,
                    "host": parsed_db.hostname or "127.0.0.1",
                    "port": parsed_db.port or 5432,
                    "token": approval_token,
                    "token_sha256": token_sha256,
                    "actions": ["create", "cleanup"],
                }
                temp_approval_file.write_text(
                    json.dumps(temp_approval_data, indent=2), encoding="utf-8"
                )

                # Pass token through environment variable whose value is NOT recorded in command_log
                race_env = {
                    **os.environ,
                    "RACE_LAB_APPROVAL_TOKEN": approval_token,
                }
                cmd = [
                    str(backend_python),
                    "scripts/race_demo.py",
                    "--admin-url",
                    admin_url,
                    "--run-id",
                    lab_run_id,
                    "--approval-file",
                    str(temp_approval_file),
                    "--evidence-dir",
                    str(evidence_dir),
                ]
                code = run_command(
                    cmd,
                    evidence_dir,
                    len(command_log) + 1,
                    env=race_env,
                )
                sanitized_cmd = sanitize_command(cmd)
                command_log.append({"cmd": sanitized_cmd, "exit_code": code})
                if code != 0:
                    return code

            clean_admin_url = re.sub(
                r"^postgresql\+asyncpg://", "postgresql://", admin_url
            )
            cleanup_script = (
                "import asyncio, os, sys, asyncpg\n"
                "async def check():\n"
                "    dsn = os.environ.get('CLEANUP_VERIFY_DSN')\n"
                "    if not dsn:\n"
                "        sys.exit('Missing CLEANUP_VERIFY_DSN')\n"
                "    target_db = sys.argv[1]\n"
                "    try:\n"
                "        conn = await asyncpg.connect(dsn)\n"
                "    except Exception:\n"
                "        sys.exit('Database connection failed during cleanup verification')\n"
                "    try:\n"
                "        exists = await conn.fetchval(\n"
                "            'SELECT 1 FROM pg_database WHERE datname = $1', target_db\n"
                "        )\n"
                "    except Exception:\n"
                "        sys.exit('Database query failed during cleanup verification')\n"
                "    finally:\n"
                "        try:\n"
                "            await conn.close()\n"
                "        except Exception:\n"
                "            pass\n"
                "    if exists:\n"
                "        sys.exit(f'Disposable database {target_db} still exists after cleanup')\n"
                "    print(f'Verified ephemeral database {target_db} cleaned up')\n"
                "asyncio.run(check())\n"
            )
            verify_cleanup_cmd = [
                str(backend_python),
                "-c",
                cleanup_script,
                lab_db_name,
            ]
            cleanup_env = {
                **os.environ,
                "CLEANUP_VERIFY_DSN": clean_admin_url,
            }
            clean_code = run_command(
                verify_cleanup_cmd,
                evidence_dir,
                len(command_log) + 1,
                env=cleanup_env,
            )
            command_log.append(
                {"cmd": sanitize_command(verify_cleanup_cmd), "exit_code": clean_code}
            )
            return clean_code
        finally:
            db_mgr.stop(command_log)

    elif target == "regression":
        regression_order = central_manifest.get("ordered_targets", [])
        implemented_targets = set(central_manifest.get("implemented_targets", []))

        for gate in regression_order:
            if gate in implemented_targets:
                print(f"\n=== Running regression gate: {gate} ===")
                code = run_target(
                    gate,
                    None,
                    central_manifest,
                    fragments,
                    evidence_dir,
                    command_log,
                    run_id,
                )
                if code != 0:
                    return code
            else:
                print(f"\n=== Skipping unimplemented regression gate: {gate} ===")
        return 0

    elif target == "ticket":
        if not ticket_id:
            sys.exit("Error: Target 'ticket' requires --ticket <id>")
        if ticket_id not in fragments:
            sys.exit(f"Error: Unknown ticket identifier: {ticket_id}")

        fragment = fragments[ticket_id]
        pytest_paths = fragment["pytest_paths"]
        vitest_paths = fragment["vitest_paths"]
        playwright_paths = fragment["playwright_paths"]
        doc_checks = fragment["document_checks"]

        needs_db = any("integration" in p or "concurrency" in p for p in pytest_paths)
        db_mgr = (
            IsolatedDatabaseManager(evidence_dir=evidence_dir, run_id=run_id)
            if needs_db
            else None
        )

        try:
            test_env = dict(os.environ)
            if db_mgr:
                database_url = db_mgr.start(command_log)
                test_env["DATABASE_URL"] = database_url
                test_env["TEST_RUN_ID"] = db_mgr.test_run_id

            if pytest_paths:
                cmd = [uv_bin, "run", "--project", "backend", "pytest"] + pytest_paths
                code = run_command(
                    cmd,
                    evidence_dir,
                    len(command_log) + 1,
                    env=test_env,
                    check_tool="pytest",
                )
                command_log.append({"cmd": cmd, "exit_code": code})
                if code != 0:
                    return code

            if vitest_paths:
                transformed_paths: list[str] = []
                for vp in vitest_paths:
                    p = Path(vp)
                    if p.parts and p.parts[0] == "frontend":
                        transformed_paths.append(str(Path(*p.parts[1:])))
                    else:
                        transformed_paths.append(vp)
                cmd = [
                    npm_bin,
                    "--prefix",
                    "frontend",
                    "test",
                    "--",
                    "--run",
                ] + transformed_paths
                code = run_command(
                    cmd,
                    evidence_dir,
                    len(command_log) + 1,
                    check_tool="vitest",
                )
                command_log.append({"cmd": cmd, "exit_code": code})
                if code != 0:
                    return code

            if playwright_paths:
                sys.exit(
                    "Error: Playwright verification paths not implemented in this version"
                )

            if doc_checks:
                sys.exit(
                    "Error: Document verification checks not implemented in this version"
                )

            return 0
        finally:
            if db_mgr:
                db_mgr.stop(command_log)

    elif target == "build":
        docker_bin = find_tool("docker")
        cmd = [docker_bin, "build", "-f", "infra/Dockerfile.api", "."]
        code = run_command(cmd, evidence_dir, len(command_log) + 1)
        command_log.append({"cmd": cmd, "exit_code": code})
        return code

    elif target in central_manifest.get("implemented_targets", []):
        sys.exit(
            f"Error: Target '{target}' is implemented in manifest but has no runner logic"
        )

    else:
        sys.exit(f"Error: Target '{target}' is not implemented")


def scan_evidence_for_leaks(
    evidence_dir: Path, known_secrets: list[str] | None = None
) -> list[str]:
    """Scan all files in evidence directory for credential leaks or unmasked secrets."""
    violations: list[str] = []
    uri_password_pattern = re.compile(r"://[^:\s\'\"]+:([^@\s\'\"]+)@")
    if not evidence_dir.exists():
        return violations
    for file_path in sorted(evidence_dir.rglob("*")):
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for match in uri_password_pattern.finditer(content):
            pw = match.group(1)
            if pw != "***":
                rel = file_path.relative_to(evidence_dir)
                violations.append(f"Unmasked password in {rel}")
        if known_secrets:
            for sec in known_secrets:
                if sec and len(sec) >= 6 and sec in content:
                    rel = file_path.relative_to(evidence_dir)
                    violations.append(f"Known secret leaked in {rel}")
    return violations


def execute_gate(target, ticket, central, fragments, evidence_dir, run_id, fresh=False):
    """Execute once or reuse a validated original run; always preserve failure evidence."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    versions = get_tool_versions()
    if target not in {"integration", "concurrency", "permissions", "build", "race-lab"}:
        for name in ("docker", "compose", "postgres_image"):
            versions.pop(name, None)
    else:
        for name in ("node", "npm", "frontend_packages"):
            versions.pop(name, None)
    sha = get_git_sha()
    fingerprint = compute_fingerprint(target)
    profile = (
        "race"
        if target == "concurrency"
        else os.environ.get("TEST_PROFILE", "standard")
    )
    root = Path(os.environ.get("EVIDENCE_ROOT", str(REPO_ROOT / "evidence")))
    if (
        is_evidence_reuse_activated(central)
        and target in get_reusable_gates(central)
        and not ticket
    ):
        eligible, reason, match = check_reuse_eligibility(
            target, fingerprint, versions, profile, root, fresh=fresh
        )
        if eligible and match:
            source, source_path = match
            data = create_reused_manifest(
                target, source, source_path, sha, fingerprint, run_id, timestamp
            )
            data["duration_seconds"] = time.perf_counter() - started
            (evidence_dir / "manifest.json").write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            print(f"{target}: reused original execution {source_path}")
            return 0
        print(f"{target}: executing ({reason})")
    commands = []
    error = None
    try:
        code = run_target(
            target, ticket, central, fragments, evidence_dir, commands, run_id
        )
    except (Exception, SystemExit) as exc:
        code = 1
        error = str(exc)
        print(error, file=sys.stderr)
    finally:
        IsolatedDatabaseManager._cleanup_all()
    if any(entry["exit_code"] != 0 for entry in commands):
        code = code or 1
    if compute_fingerprint(target) != fingerprint:
        code, error = 1, "Inputs changed during execution"
    records = []
    for index, entry in enumerate(commands, 1):
        record = dict(entry)
        if "cmd" in record and isinstance(record["cmd"], list):
            record["cmd"] = sanitize_command(record["cmd"])
        for stream in ("stdout", "stderr"):
            name = f"cmd_{index:02d}_{stream}.txt"
            path = evidence_dir / name
            record[f"{stream}_file"] = name
            record[f"{stream}_sha256"] = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else None
            )
        records.append(record)
    leak_violations = scan_evidence_for_leaks(evidence_dir)
    if leak_violations:
        code = 1
        leak_msg = f"Security violation: credential leak detected in evidence: {', '.join(leak_violations)}"
        error = f"{error}; {leak_msg}" if error else leak_msg
        print(f"Error: {leak_msg}", file=sys.stderr)

    data = {
        "evidence_version": 2,
        "timestamp": timestamp,
        "run_id": run_id,
        "execution_type": "fresh",
        "reused": False,
        "git_sha": sha,
        "input_fingerprint": fingerprint,
        "configuration_hash": compute_configuration_hash(),
        "tool_versions": versions,
        "test_profile": profile,
        "target": target,
        "ticket": ticket,
        "commands": records,
        "exit_code": code,
        "error": error,
        "duration_seconds": time.perf_counter() - started,
    }
    (evidence_dir / "manifest.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    print(f"{target}: exit {code}; evidence {evidence_dir}")
    return code


def changed_scope(base):
    """Unknown inputs require complete verification. Documentation never starts a database."""
    proc = subprocess.run(
        [find_tool("git"), "diff", "--name-only", base, "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        raise RuntimeError("Cannot resolve comparison base")
    paths = proc.stdout.splitlines()
    if paths and all(
        p.endswith(".md") and (p.startswith("docs/") or p == "README.md") for p in paths
    ):
        return "docs"
    if paths and all(p.startswith("frontend/") or p.endswith(".md") for p in paths):
        return "frontend"
    return "all"


def main() -> None:
    parser = argparse.ArgumentParser(description="Repository verification runner")
    parser.add_argument("target")
    parser.add_argument("--ticket")
    parser.add_argument("--fresh", action="store_true", help="Force fresh execution")
    parser.add_argument(
        "--base",
        help="CI comparison base; selects documentation/frontend/full regression",
    )
    args = parser.parse_args()
    central, fragments = load_manifests()
    if args.target not in RECOGNIZED_TARGETS:
        sys.exit(f"Error: Unrecognized target '{args.target}'")
    if args.target not in central["implemented_targets"]:
        sys.exit(f"Error: Target '{args.target}' is not implemented")
    run_id = (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:12]
    )
    evidence_dir = Path(
        os.environ.get("EVIDENCE_DIR", str(REPO_ROOT / "evidence" / run_id))
    )
    if evidence_dir.exists() and any(evidence_dir.iterdir()):
        sys.exit("Evidence directory must be new or empty")
    if args.target == "regression":
        scope = changed_scope(args.base) if args.base else "all"
        if scope == "docs":
            evidence_dir.mkdir(parents=True, exist_ok=True)
            check = subprocess.run(
                [find_tool("git"), "diff", "--check", args.base, "HEAD"], cwd=REPO_ROOT
            )
            (evidence_dir / "scope.json").write_text(
                json.dumps(
                    {
                        "scope": scope,
                        "base": args.base,
                        "sha": get_git_sha(),
                        "exit_code": check.returncode,
                    }
                ),
                encoding="utf-8",
            )
            sys.exit(check.returncode)
        gates = ["frontend"] if scope == "frontend" else central["ordered_targets"]
        for gate in gates:
            if gate not in central["implemented_targets"]:
                print(f"{gate}: not implemented; no passing evidence claimed")
                continue
            code = execute_gate(
                gate,
                None,
                central,
                fragments,
                evidence_dir / gate,
                run_id + "_" + gate,
                args.fresh,
            )
            if code:
                sys.exit(code)
    else:
        code = execute_gate(
            args.target,
            args.ticket,
            central,
            fragments,
            evidence_dir,
            run_id,
            args.fresh,
        )
        if code:
            sys.exit(code)


if __name__ == "__main__":
    main()
