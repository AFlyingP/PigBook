#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

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


def run_command(
    cmd: list[str],
    evidence_dir: Path,
    cmd_index: int,
    cwd: Path | None = None,
) -> int:
    display_cwd = str(cwd) if cwd else str(REPO_ROOT)
    print(f"[{cmd_index}] Running: {' '.join(cmd)} (cwd: {display_cwd})")
    
    proc = subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
        
    stdout_file = evidence_dir / f"cmd_{cmd_index:02d}_stdout.txt"
    stderr_file = evidence_dir / f"cmd_{cmd_index:02d}_stderr.txt"
    
    stdout_file.write_text(proc.stdout, encoding="utf-8")
    stderr_file.write_text(proc.stderr, encoding="utf-8")
    
    return proc.returncode


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


def load_manifests() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_path = REPO_ROOT / "scripts" / "verification.json"
    if not manifest_path.exists():
        sys.exit("Error: scripts/verification.json not found")
    
    central_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    
    fragments_dir = REPO_ROOT / "scripts" / "verification.d"
    fragments: dict[str, dict[str, Any]] = {}
    
    if fragments_dir.exists():
        for frag_path in sorted(fragments_dir.glob("*.json")):
            data = json.loads(frag_path.read_text(encoding="utf-8"))
            
            # Fragment schema validation
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
                
            # Path and check validation
            pytest_paths = data["pytest_paths"]
            vitest_paths = data["vitest_paths"]
            playwright_paths = data["playwright_paths"]
            doc_checks = data["document_checks"]
            
            if not isinstance(pytest_paths, list) or not isinstance(vitest_paths, list) or                not isinstance(playwright_paths, list) or not isinstance(doc_checks, list):
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
) -> int:
    uv_bin = find_tool("uv")
    npm_bin = find_tool("npm")
    
    if target == "bootstrap":
        # Ensure uv exists
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

        # Sync backend dependencies using lockfile
        sync_cmd = [uv_bin, "sync", "--frozen", "--python", "3.12", "--project", "backend"]
        code = run_command(sync_cmd, evidence_dir, len(command_log) + 1)
        command_log.append({"cmd": sync_cmd, "exit_code": code})
        if code != 0:
            return code

        # Install frontend dependencies using package-lock
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
        unit_commands = [
            [uv_bin, "run", "--project", "backend", "pytest", "backend/tests/unit"],
            [npm_bin, "--prefix", "frontend", "test", "--", "--run"],
        ]
        for cmd in unit_commands:
            code = run_command(cmd, evidence_dir, len(command_log) + 1)
            command_log.append({"cmd": cmd, "exit_code": code})
            if code != 0:
                return code
        return 0

    elif target == "regression":
        regression_order = central_manifest.get("ordered_targets", [])
        implemented_targets = set(central_manifest.get("implemented_targets", []))
        
        for gate in regression_order:
            if gate in implemented_targets:
                print(f"\n=== Running regression gate: {gate} ===")
                code = run_target(gate, None, central_manifest, fragments, evidence_dir, command_log)
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
        
        if pytest_paths:
            cmd = [uv_bin, "run", "--project", "backend", "pytest"] + pytest_paths
            code = run_command(cmd, evidence_dir, len(command_log) + 1)
            command_log.append({"cmd": cmd, "exit_code": code})
            if code != 0:
                return code
                
        if vitest_paths:
            # Transform paths relative to frontend directory if needed
            transformed_paths: list[str] = []
            for vp in vitest_paths:
                p = Path(vp)
                if p.parts and p.parts[0] == "frontend":
                    transformed_paths.append(str(Path(*p.parts[1:])))
                else:
                    transformed_paths.append(vp)
            cmd = [npm_bin, "--prefix", "frontend", "test", "--", "--run"] + transformed_paths
            code = run_command(cmd, evidence_dir, len(command_log) + 1)
            command_log.append({"cmd": cmd, "exit_code": code})
            if code != 0:
                return code
                
        if playwright_paths:
            sys.exit("Error: Playwright verification paths not implemented in this version")
            
        if doc_checks:
            sys.exit("Error: Document verification checks not implemented in this version")
            
        return 0

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
    
    # Check if target is implemented
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
    
    command_log: list[dict[str, Any]] = []
    
    exit_code = run_target(
        target,
        args.ticket,
        central_manifest,
        fragments,
        evidence_dir,
        command_log,
    )
    
    # Write evidence manifest
    manifest_data = {
        "timestamp": utc_now.isoformat(),
        "run_id": run_id,
        "git_sha": get_git_sha(),
        "configuration_hash": compute_configuration_hash(),
        "tool_versions": get_tool_versions(),
        "target": target,
        "ticket": args.ticket,
        "fresh": args.fresh,
        "reuse_cache": None,
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
