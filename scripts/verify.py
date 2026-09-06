#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
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


def run_command(
    cmd: list[str],
    evidence_dir: Path,
    cmd_index: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check_tool: str | None = None,
) -> int:
    display_cwd = str(cwd) if cwd else str(REPO_ROOT)
    print(f"[{cmd_index}] Running: {' '.join(cmd)} (cwd: {display_cwd})")

    proc = subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)

    return_code = proc.returncode

    # If the process exited 0, check for skips or zero collected tests
    if return_code == 0 and check_tool:
        skip_err = check_no_skips(proc.stdout, check_tool)
        if skip_err:
            print(f"\nError: {skip_err}", file=sys.stderr)
            return_code = 1

    stdout_file = evidence_dir / f"cmd_{cmd_index:02d}_stdout.txt"
    stderr_file = evidence_dir / f"cmd_{cmd_index:02d}_stderr.txt"

    stdout_file.write_text(proc.stdout, encoding="utf-8")
    stderr_file.write_text(proc.stderr, encoding="utf-8")

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

    def __init__(self, evidence_dir: Path, run_id: str) -> None:
        self.evidence_dir = evidence_dir
        self.test_run_id = os.environ.get("TEST_RUN_ID") or run_id
        # Derive deterministic, sanitized project and database identifiers
        slug = hashlib.sha256(self.test_run_id.encode("utf-8")).hexdigest()[:12]
        self.project_name = f"cb_test_{slug}"
        self.db_name = f"cb_test_{slug}"
        self.db_user = "commonsbook_test"
        self.db_password = "commonsbook_test"

        port_offset = int(hashlib.sha256(f"port_{self.test_run_id}".encode("utf-8")).hexdigest(), 16) % 2000
        base_port = 15432 + port_offset
        self.allocated_port = find_free_port(base_port)
        self.docker_bin = find_tool("docker")

    def start(self, command_log: list[dict[str, Any]]) -> str:
        override_yaml = (
            f"services:\n"
            f"  db:\n"
            f"    ports:\n"
            f"      - \"127.0.0.1:{self.allocated_port}:5432\"\n"
        )
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
        print(f"Starting isolated test database in namespace '{self.project_name}' on port {self.allocated_port}...")
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
        command_log.append({
            "cmd": cmd + [f"(stdin: port 127.0.0.1:{self.allocated_port}:5432)"],
            "exit_code": proc.returncode,
        })
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            raise RuntimeError(f"Failed to start isolated database container: {proc.stderr}")

        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@127.0.0.1:{self.allocated_port}/{self.db_name}"

    def stop(self, command_log: list[dict[str, Any]]) -> None:
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
        print(f"Tearing down isolated test database in namespace '{self.project_name}'...")
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        command_log.append({"cmd": cmd, "exit_code": proc.returncode})


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
                sys.exit(f"Error: Fragment {frag_path.name} must have exactly keys: {required_keys}")

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

            total_checks = len(pytest_paths) + len(vitest_paths) + len(playwright_paths) + len(doc_checks)
            if total_checks == 0:
                sys.exit(f"Error: Fragment {frag_path.name} must declare at least one check")

            for dc in doc_checks:
                if dc not in LEGAL_DOCUMENT_CHECKS:
                    sys.exit(f"Error: Illegal document check '{dc}' in {frag_path.name}")

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

        sync_cmd = [uv_bin, "sync", "--frozen", "--python", "3.12", "--project", "backend"]
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
            [uv_bin, "run", "--project", "backend", "ruff", "format", "--check", "backend"],
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

    elif target == "integration":
        db_mgr = IsolatedDatabaseManager(evidence_dir=evidence_dir, run_id=run_id)
        try:
            database_url = db_mgr.start(command_log)
            test_env = {
                **os.environ,
                "DATABASE_URL": database_url,
                "TEST_RUN_ID": db_mgr.test_run_id,
            }
            cmd = [uv_bin, "run", "--project", "backend", "pytest", "backend/tests/integration"]
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

        needs_db = any("integration" in p for p in pytest_paths)
        db_mgr = IsolatedDatabaseManager(evidence_dir=evidence_dir, run_id=run_id) if needs_db else None

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
                cmd = [npm_bin, "--prefix", "frontend", "test", "--", "--run"] + transformed_paths
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
                sys.exit("Error: Playwright verification paths not implemented in this version")

            if doc_checks:
                sys.exit("Error: Document verification checks not implemented in this version")

            return 0
        finally:
            if db_mgr:
                db_mgr.stop(command_log)

    elif target in central_manifest.get("implemented_targets", []):
        sys.exit(f"Error: Target '{target}' is implemented in manifest but has no runner logic")

    else:
        sys.exit(f"Error: Target '{target}' is not implemented")


def main() -> None:
    parser = argparse.ArgumentParser(description="Repository verification runner")
    parser.add_argument("target", help="Verification target to execute")
    parser.add_argument("--ticket", help="Target ticket identifier", default=None)
    parser.add_argument("--fresh", action="store_true", help="Force fresh execution and disable reuse")

    args = parser.parse_args()
    target = args.target

    if target not in RECOGNIZED_TARGETS:
        sys.exit(f"Error: Unrecognized target '{target}'")

    central_manifest, fragments = load_manifests()

    implemented_targets = set(central_manifest.get("implemented_targets", []))
    if target != "regression" and target not in implemented_targets:
        sys.exit(f"Error: Target '{target}' is not implemented")

    utc_now = datetime.datetime.now(datetime.timezone.utc)
    utc_timestamp = utc_now.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{utc_timestamp}_{os.getpid()}"

    env_evidence_dir = os.environ.get("EVIDENCE_DIR")
    if env_evidence_dir:
        evidence_dir = Path(env_evidence_dir)
    else:
        evidence_dir = REPO_ROOT / "evidence" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    tool_versions = get_tool_versions()
    git_sha = get_git_sha()
    candidate_fingerprint = compute_fingerprint(target)
    test_profile = os.environ.get("TEST_PROFILE", "standard")

    # Check for truthful evidence reuse when applicable
    reuse_activated = is_evidence_reuse_activated(central_manifest)
    reusable_gates = set(get_reusable_gates(central_manifest))

    if (
        reuse_activated
        and not args.fresh
        and target in reusable_gates
        and not args.ticket
    ):
        env_evidence_root = os.environ.get("EVIDENCE_ROOT")
        evidence_root = Path(env_evidence_root) if env_evidence_root else REPO_ROOT / "evidence"
        eligible, reason, match = check_reuse_eligibility(
            gate=target,
            candidate_fingerprint=candidate_fingerprint,
            candidate_tools=tool_versions,
            test_profile=test_profile,
            evidence_root=evidence_root,
            fresh=args.fresh,
        )
        if eligible and match:
            source_data, source_path = match
            print(f"\n[CACHE] Evidence reuse eligible: {reason}")
            print(f"[CACHE] Reusing manifest from {source_path} (reused, never rerun)")
            reused_manifest = create_reused_manifest(
                gate=target,
                source_manifest=source_data,
                source_manifest_path=source_path,
                candidate_sha=git_sha,
                candidate_fingerprint=candidate_fingerprint,
                run_id=run_id,
                timestamp=utc_now.isoformat(),
            )
            manifest_file = evidence_dir / "manifest.json"
            manifest_file.write_text(json.dumps(reused_manifest, indent=2), encoding="utf-8")
            print(f"\nVerification '{target}' passed (reused). Evidence saved to {evidence_dir}")
            return

    command_log: list[dict[str, Any]] = []

    exit_code = run_target(
        target,
        args.ticket,
        central_manifest,
        fragments,
        evidence_dir,
        command_log,
        run_id,
    )

    manifest_data = {
        "timestamp": utc_now.isoformat(),
        "run_id": run_id,
        "reused": False,
        "execution_type": "fresh",
        "git_sha": git_sha,
        "input_fingerprint": candidate_fingerprint,
        "configuration_hash": compute_configuration_hash(),
        "tool_versions": tool_versions,
        "test_profile": test_profile,
        "target": target,
        "ticket": args.ticket,
        "fresh": args.fresh,
        "commands": [
            {
                "cmd": entry["cmd"],
                "exit_code": entry["exit_code"],
                "stdout_file": f"cmd_{idx+1:02d}_stdout.txt",
                "stderr_file": f"cmd_{idx+1:02d}_stderr.txt",
            }
            for idx, entry in enumerate(command_log)
        ],
        "exit_code": exit_code,
    }

    manifest_file = evidence_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    if exit_code != 0:
        sys.exit(exit_code)
    print(f"\nVerification '{target}' passed. Evidence saved to {evidence_dir}")


if __name__ == "__main__":
    main()
