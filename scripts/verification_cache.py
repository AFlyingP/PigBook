#!/usr/bin/env python3
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIR_NAMES = {
    ".venv",
    "node_modules",
    "__pycache__",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".git",
    "evidence",
    "dist",
    "build",
    ".vite",
}

EXCLUDED_FILE_PATTERNS = {
    ".DS_Store",
}


def should_exclude(rel_path: Path) -> bool:
    for part in rel_path.parts:
        if part in EXCLUDED_DIR_NAMES:
            return True
    name = rel_path.name
    if (
        name in EXCLUDED_FILE_PATTERNS
        or name.endswith(".pyc")
        or name.endswith(".tsbuildinfo")
    ):
        return True
    return False


def get_gate_inputs(gate: str, root: Path) -> list[Path]:
    """Collect relevant input files for a gate in sorted order."""
    files: set[Path] = set()

    def add_tree(dir_path: Path) -> None:
        if not dir_path.exists():
            return
        for current, dirs, names in os.walk(dir_path):
            dirs[:] = [name for name in dirs if name not in EXCLUDED_DIR_NAMES]
            for name in names:
                p = Path(current) / name
                if p.is_file() and not should_exclude(p.relative_to(root)):
                    files.add(p)

    def add_file(f_path: Path) -> None:
        if f_path.exists() and f_path.is_file():
            rel = f_path.relative_to(root)
            if not should_exclude(rel):
                files.add(f_path)

    def add_glob(pattern: str) -> None:
        for p in root.glob(pattern):
            if p.is_file():
                rel = p.relative_to(root)
                if not should_exclude(rel):
                    files.add(p)

    # Core manifests and locks common to verification
    add_file(root / "backend" / "pyproject.toml")
    add_file(root / "backend" / "uv.lock")
    add_tree(root / "scripts")
    add_tree(root / ".github")
    add_file(root / ".env")
    add_file(root / "docs" / "schema.sql")
    add_file(root / "docs" / "openapi.json")
    add_file(root / "openapi.json")
    if gate not in ("integration", "concurrency", "permissions", "race-lab"):
        add_tree(root / "frontend")
    add_file(root / "scripts" / "verification.json")
    add_tree(root / "scripts" / "verification.d")

    if gate in ("concurrency", "permissions", "race-lab"):
        add_tree(root / "backend")
        add_tree(root / "scripts")
        add_glob("compose*.yaml")
        add_glob("compose*.yml")
        add_tree(root / "infra")
        add_tree(root / ".github")
        add_file(root / "schema.sql")
        add_file(root / "openapi.json")
    elif gate == "lint":
        add_tree(root / "backend")
        add_tree(root / "frontend" / "src")
        add_tree(root / "frontend" / "tests")
        add_file(root / "frontend" / "eslint.config.js")
        add_file(root / "frontend" / "tsconfig.json")
        add_file(root / "frontend" / "vite.config.ts")
        add_file(root / "frontend" / "vitest.config.ts")
    elif gate == "unit":
        add_file(root / "backend" / "tests" / "conftest.py")
        add_file(root / "backend" / "tests" / "factories.py")
        add_tree(root / "backend" / "app")
        add_tree(root / "backend" / "tests" / "unit")
        add_tree(root / "frontend" / "src")
        add_tree(root / "frontend" / "tests")
        add_file(root / "frontend" / "vitest.config.ts")
    elif gate == "integration":
        add_tree(root / "backend")
        add_tree(root / "scripts")
        add_glob("compose*.yaml")
        add_glob("compose*.yml")
        add_tree(root / "infra")
    else:
        # Fallback for targets
        add_tree(root / "backend")
        add_tree(root / "scripts")
        add_glob("compose*.yaml")
        add_glob("compose*.yml")
        add_tree(root / "infra")

    return sorted(list(files), key=lambda p: p.relative_to(root).as_posix())


def compute_fingerprint(gate: str, root_dir: Path | None = None) -> str:
    """
    Compute deterministic SHA-256 fingerprint for a gate's input set.
    Includes relative path, tracked file mode, and byte contents in sorted order.
    Timestamps do not participate.
    """
    root = (root_dir or REPO_ROOT).resolve()
    input_files = get_gate_inputs(gate, root)

    hasher = hashlib.sha256()
    hasher.update(gate.encode("utf-8"))
    hasher.update(b"\x00")

    for f in input_files:
        rel_posix = f.relative_to(root).as_posix()
        st = f.stat()
        mode = stat.S_IMODE(st.st_mode)
        content = f.read_bytes()

        hasher.update(rel_posix.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(mode).encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(content)
        hasher.update(b"\x00")

    return hasher.hexdigest()


def check_reuse_eligibility(
    gate: str,
    candidate_fingerprint: str,
    candidate_tools: dict[str, str],
    test_profile: str,
    evidence_root: Path,
    fresh: bool = False,
    candidate_locks: dict[str, str] | None = None,
) -> tuple[bool, str, tuple[dict[str, Any], Path] | None]:
    """
    Check if an existing successful verification evidence manifest can be reused.
    Returns: (is_eligible, reason, (source_manifest_data, source_manifest_path) | None)
    """
    if fresh:
        return False, "Fresh execution requested (--fresh)", None

    if not evidence_root.exists():
        return False, "No evidence directory found", None

    manifest_paths = [p for p in evidence_root.rglob("manifest.json") if p.is_file()]
    manifest_paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for m_path in manifest_paths:
        try:
            data = json.loads(m_path.read_text(encoding="utf-8"))
        except Exception:
            return False, "Invalid evidence manifest; fresh execution required", None

        if not isinstance(data, dict):
            return False, "Invalid evidence manifest", None

        if data.get("target") != gate:
            continue

        if data.get("reused") is True:
            continue  # Always validate the original execution, never a chain of claims.

        if type(data.get("exit_code")) is not int or data["exit_code"] != 0:
            return (
                False,
                "Source run failed; cannot reuse failed execution evidence",
                None,
            )

        source_fp = data.get("input_fingerprint")
        if not source_fp or source_fp != candidate_fingerprint:
            return (
                False,
                f"Fingerprint mismatch: candidate {candidate_fingerprint} != source {source_fp}",
                None,
            )

        source_tools = data.get("tool_versions", {})
        if source_tools != candidate_tools:
            return False, "Tool versions do not match source run", None

        source_profile = data.get("test_profile", "standard")
        if source_profile != test_profile:
            return False, "Test profile mismatch", None

        if data.get("evidence_version") != 2 or data.get("execution_type") != "fresh":
            return False, "Missing validated execution provenance", None
        if not isinstance(data.get("git_sha"), str) or not re.fullmatch(
            r"[0-9a-f]{40}", data["git_sha"]
        ):
            return False, "Missing source SHA", None
        commands = data.get("commands")
        if not isinstance(commands, list) or not commands:
            return False, "Missing executed commands", None
        if not candidate_tools or any(
            value in ("unknown", "not-found", "") for value in candidate_tools.values()
        ):
            return False, "Uncertain tool versions", None
        for command in commands:
            if not isinstance(command, dict) or command.get("exit_code") != 0:
                return False, "Failed or invalid child command", None
            for stream in ("stdout", "stderr"):
                name = command.get(f"{stream}_file", "")
                if not isinstance(name, str):
                    return False, "Invalid command artifact path", None
                artifact = (m_path.parent / name).resolve()
                if (
                    not name
                    or not artifact.is_relative_to(m_path.parent.resolve())
                    or not artifact.is_file()
                ):
                    return False, "Missing command artifact", None
                if hashlib.sha256(artifact.read_bytes()).hexdigest() != command.get(
                    f"{stream}_sha256"
                ):
                    return False, "Invalid command artifact hash", None

        if candidate_locks and "dependency_locks" in data:
            if data["dependency_locks"] != candidate_locks:
                return False, "Dependency lock mismatch", None

        return True, "Valid matching execution evidence found for reuse", (data, m_path)

    return False, "No prior matching evidence found", None


def create_reused_manifest(
    gate: str,
    source_manifest: dict[str, Any],
    source_manifest_path: Path,
    candidate_sha: str,
    candidate_fingerprint: str,
    run_id: str,
    timestamp: str,
) -> dict[str, Any]:
    source_bytes = source_manifest_path.read_bytes()
    source_manifest_hash = hashlib.sha256(source_bytes).hexdigest()

    source_sha = source_manifest.get("source_verified_sha") or source_manifest.get(
        "git_sha", "unknown"
    )

    return {
        "timestamp": timestamp,
        "run_id": run_id,
        "reused": True,
        "execution_type": "reused",
        "label": "reused, never rerun",
        "target": gate,
        "source_verified_sha": source_sha,
        "candidate_sha": candidate_sha,
        "source_manifest_hash": source_manifest_hash,
        "source_manifest_path": str(source_manifest_path.resolve()),
        "input_fingerprint": candidate_fingerprint,
        "tool_versions": source_manifest.get("tool_versions", {}),
        "test_profile": source_manifest.get("test_profile", "standard"),
        "commands": source_manifest.get("commands", []),
        "exit_code": 0,
    }


def is_evidence_reuse_activated(
    manifest: dict[str, Any] | None = None,
    root_dir: Path | None = None,
) -> bool:
    """Return whether evidence reuse is activated in the manifest."""
    if manifest is None:
        root = (root_dir or REPO_ROOT).resolve()
        manifest_file = root / "scripts" / "verification.json"
        if manifest_file.exists():
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except Exception:
                return False
        else:
            return False
    return bool(manifest.get("evidence_reuse", {}).get("activated", False))


def get_reusable_gates(
    manifest: dict[str, Any] | None = None,
    root_dir: Path | None = None,
) -> list[str]:
    """Return the list of gates eligible for reuse once activated."""
    if manifest is None:
        root = (root_dir or REPO_ROOT).resolve()
        manifest_file = root / "scripts" / "verification.json"
        if manifest_file.exists():
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except Exception:
                return []
        else:
            return []
    return list(manifest.get("evidence_reuse", {}).get("reusable_gates", []))
