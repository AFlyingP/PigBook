import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verify  # noqa: E402


def test_unimplemented_target_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["verify.py", "smoke"])
    with pytest.raises(SystemExit) as exc_info:
        verify.main()
    assert exc_info.value.code != 0
    assert "not implemented" in str(exc_info.value)


def test_manifest_fragment_declaring_nonexistent_path_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_file = tmp_path / "verification.json"
    manifest_file.write_text(
        json.dumps(
            {
                "ordered_targets": [],
                "implemented_targets": ["bootstrap"],
                "implemented_endpoints": {},
                "tooling": {},
            }
        ),
        encoding="utf-8",
    )

    frag_dir = tmp_path / "verification.d"
    frag_dir.mkdir()
    bad_frag = frag_dir / "T-998.json"
    bad_frag.write_text(
        json.dumps(
            {
                "ticket_id": "T-998",
                "pytest_paths": ["nonexistent/path/for/test.py"],
                "vitest_paths": [],
                "playwright_paths": [],
                "document_checks": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        verify.load_manifests(manifest_path=manifest_file, fragments_dir=frag_dir)
    assert "does not exist" in str(exc_info.value)


def test_fragment_with_all_empty_check_arrays_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_file = tmp_path / "verification.json"
    manifest_file.write_text(
        json.dumps(
            {
                "ordered_targets": [],
                "implemented_targets": ["bootstrap"],
                "implemented_endpoints": {},
                "tooling": {},
            }
        ),
        encoding="utf-8",
    )

    frag_dir = tmp_path / "verification.d"
    frag_dir.mkdir()
    empty_frag = frag_dir / "T-997.json"
    empty_frag.write_text(
        json.dumps(
            {
                "ticket_id": "T-997",
                "pytest_paths": [],
                "vitest_paths": [],
                "playwright_paths": [],
                "document_checks": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        verify.load_manifests(manifest_path=manifest_file, fragments_dir=frag_dir)
    assert "declare at least one check" in str(exc_info.value)


def test_markdown_path_in_pytest_paths_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_file = tmp_path / "verification.json"
    manifest_file.write_text(
        json.dumps(
            {
                "ordered_targets": [],
                "implemented_targets": ["bootstrap"],
                "implemented_endpoints": {},
                "tooling": {},
            }
        ),
        encoding="utf-8",
    )

    frag_dir = tmp_path / "verification.d"
    frag_dir.mkdir()
    md_frag = frag_dir / "T-996.json"
    md_frag.write_text(
        json.dumps(
            {
                "ticket_id": "T-996",
                "pytest_paths": ["docs/testing.md"],
                "vitest_paths": [],
                "playwright_paths": [],
                "document_checks": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        verify.load_manifests(manifest_path=manifest_file, fragments_dir=frag_dir)
    assert "forbidden in pytest_paths" in str(exc_info.value)


def test_duplicate_ticket_id_across_fragments_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_file = tmp_path / "verification.json"
    manifest_file.write_text(
        json.dumps(
            {
                "ordered_targets": [],
                "implemented_targets": ["bootstrap"],
                "implemented_endpoints": {},
                "tooling": {},
            }
        ),
        encoding="utf-8",
    )

    frag_dir = tmp_path / "verification.d"
    frag_dir.mkdir()

    frag1 = frag_dir / "T-995_a.json"
    frag1.write_text(
        json.dumps(
            {
                "ticket_id": "T-995",
                "pytest_paths": ["backend/tests/unit/test_health.py"],
                "vitest_paths": [],
                "playwright_paths": [],
                "document_checks": [],
            }
        ),
        encoding="utf-8",
    )

    frag2 = frag_dir / "T-995_b.json"
    frag2.write_text(
        json.dumps(
            {
                "ticket_id": "T-995",
                "pytest_paths": ["backend/tests/unit/test_health.py"],
                "vitest_paths": [],
                "playwright_paths": [],
                "document_checks": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        verify.load_manifests(manifest_path=manifest_file, fragments_dir=frag_dir)
    assert "Duplicate identifier found: T-995" in str(exc_info.value)


def test_zero_collected_tests_treated_as_failure() -> None:
    pytest_out = (
        "collected 0 items\n\n==================== no tests ran in 0.01s ===================="
    )
    err = verify.check_no_skips(pytest_out, "pytest")
    assert err is not None
    assert "0 tests collected" in err

    vitest_out = "Tests  0 passed (0)\nDuration  10ms"
    err_vitest = verify.check_no_skips(vitest_out, "vitest")
    assert err_vitest is not None
    assert "0 tests collected" in err_vitest


def test_skipped_test_treated_as_failure() -> None:
    pytest_out = "================ 4 passed, 1 skipped in 0.23s ================"
    err = verify.check_no_skips(pytest_out, "pytest")
    assert err is not None
    assert "skipped in pytest" in err

    vitest_out = "Tests  1 skipped, 3 passed (4)"
    err_vitest = verify.check_no_skips(vitest_out, "vitest")
    assert err_vitest is not None
    assert "skipped in vitest" in err_vitest


@pytest.mark.parametrize("exit_code", [1, 7])
def test_failing_child_is_not_hidden(tmp_path: Path, exit_code: int) -> None:
    code = verify.run_command([sys.executable, "-c", f"raise SystemExit({exit_code})"], tmp_path, 1)
    assert code == exit_code
    assert (tmp_path / "cmd_01_stderr.txt").is_file()


def test_runner_exception_writes_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args):
        raise RuntimeError("child unavailable")

    monkeypatch.setattr(verify, "run_target", broken)
    monkeypatch.setattr(verify, "get_tool_versions", lambda: {"python": "3.12"})
    code = verify.execute_gate("unit", None, {}, {}, tmp_path, "failure", True)
    data = json.loads((tmp_path / "manifest.json").read_text())
    assert code != 0
    assert data["exit_code"] != 0
    assert data["error"] == "child unavailable"


def test_database_namespace_is_unique_even_with_same_requested_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_RUN_ID", "shared-value")
    first = verify.IsolatedDatabaseManager(tmp_path, "same-run")
    second = verify.IsolatedDatabaseManager(tmp_path, "same-run")
    assert first.project_name != second.project_name


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["README.md", "docs/testing.md"], "docs"),
        (["frontend/src/App.tsx"], "frontend"),
        (["backend/app/auth/dependencies.py"], "all"),
        (["scripts/verify.py"], "all"),
        (["docs/schema.sql"], "all"),
        ([], "all"),
    ],
)
def test_change_scope_is_conservative(
    monkeypatch: pytest.MonkeyPatch, paths: list[str], expected: str
) -> None:
    import subprocess

    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "\n".join(paths)),
    )
    assert verify.changed_scope("base") == expected


def test_changed_inputs_during_run_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fingerprints = iter(["before", "after"])
    monkeypatch.setattr(verify, "compute_fingerprint", lambda _: next(fingerprints))
    monkeypatch.setattr(verify, "get_tool_versions", lambda: {"python": "3.12"})
    monkeypatch.setattr(verify, "run_target", lambda *args: 0)
    assert verify.execute_gate("unit", None, {}, {}, tmp_path, "changed", True) != 0


def test_missing_implemented_suite_fails(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="has no runner logic"):
        verify.run_target(
            "smoke",
            None,
            {"implemented_targets": ["smoke"]},
            {},
            tmp_path,
            [],
            "missing",
        )


def test_race_lab_registration_and_manifest_wiring() -> None:
    manifest_path = SCRIPTS_DIR / "verification.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert "race-lab" not in manifest["ordered_targets"]
    assert manifest["ordered_targets"] == [
        "lint",
        "unit",
        "integration",
        "concurrency",
        "permissions",
    ]
    assert "race-lab" in manifest["implemented_targets"]

    runner_code = (SCRIPTS_DIR / "verify.py").read_text(encoding="utf-8")
    assert 'elif target == "race-lab":' in runner_code
    assert "scripts/race_demo.py" in runner_code
    assert "commonsbook_racelab_" in runner_code
    assert "approval.json" in runner_code
    assert "verify_cleanup" in runner_code


def test_race_lab_fingerprint_excludes_frontend() -> None:
    from verification_cache import get_gate_inputs

    inputs = get_gate_inputs("race-lab", SCRIPTS_DIR.parent)
    frontend_files = [p for p in inputs if "frontend" in p.parts]
    assert len(frontend_files) == 0, (
        f"race-lab inputs must exclude frontend, found: {frontend_files}"
    )

    backend_files = [p for p in inputs if "backend" in p.parts]
    assert len(backend_files) > 0, "race-lab inputs must include backend"


def test_t012_fragment_is_valid() -> None:
    central, fragments = verify.load_manifests()
    assert "T-012" in fragments
    t012 = fragments["T-012"]
    assert "backend/tests/integration/test_race_lab_safety.py" in t012["pytest_paths"]
    for path_str in t012["pytest_paths"]:
        assert (SCRIPTS_DIR.parent / path_str).exists()


def test_race_lab_tool_versions_retains_docker_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "get_git_sha", lambda: "1234567890123456789012345678901234567890")
    monkeypatch.setattr(verify, "run_target", lambda *a, **k: 0)
    monkeypatch.setattr(
        verify,
        "get_tool_versions",
        lambda: {
            "python_launcher": "3.12",
            "docker": "Docker 28.4.0",
            "compose": "Docker Compose 2.39.2",
            "postgres_image": "sha256:f1c337",
            "node": "v22.0.0",
            "npm": "10.0.0",
            "frontend_packages": "sha256:abc",
        },
    )

    code = verify.execute_gate("race-lab", None, {}, {}, tmp_path, "test_run", fresh=True)
    assert code == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    versions = manifest["tool_versions"]
    assert "docker" in versions
    assert "compose" in versions
    assert "postgres_image" in versions
    assert "node" not in versions
    assert "npm" not in versions


def test_sanitize_command_masks_passwords_and_redacts_tokens() -> None:
    raw_cmd = [
        "python",
        "scripts/race_demo.py",
        "--admin-url",
        "postgresql+asyncpg://commonsbook_test:super_secret_pw@127.0.0.1:15432/postgres",
        "--approval-token",
        "raw_approval_token_value_to_redact_12345",
        "--approval-token=another_secret_token_12345",
        "--evidence-dir",
        "evidence/dir",
    ]
    sanitized = verify.sanitize_command(raw_cmd)

    assert "super_secret_pw" not in " ".join(sanitized)
    assert "raw_approval_token_value_to_redact_12345" not in " ".join(sanitized)
    assert "another_secret_token_12345" not in " ".join(sanitized)
    assert "--admin-url" in sanitized
    assert "postgresql+asyncpg://commonsbook_test:***@127.0.0.1:15432/postgres" in sanitized
    assert "--approval-token" in sanitized
    assert "[REDACTED]" in sanitized
    assert "--approval-token=[REDACTED]" in sanitized


def test_race_lab_manifest_and_evidence_files_exclude_tokens_and_passwords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that all produced manifest and evidence files contain zero raw tokens or database
    passwords.
    """
    central, fragments = verify.load_manifests()
    secret_pw = "very_secret_database_password_xyz987"
    captured_tokens: list[str] = []

    def mock_start(self, command_log):
        return f"postgresql+asyncpg://commonsbook_test:{secret_pw}@127.0.0.1:16999/cb_test"

    def mock_stop(self, command_log=None):
        pass

    import subprocess

    def mock_subprocess_run(cmd, *args, **kwargs):
        # Capture raw token passed via environment variable or CLI argument
        env = kwargs.get("env") or {}
        tok = env.get("RACE_LAB_APPROVAL_TOKEN")
        if tok:
            captured_tokens.append(tok)
        for idx, arg in enumerate(cmd):
            if arg == "--approval-token" and idx + 1 < len(cmd):
                captured_tokens.append(cmd[idx + 1])
        return subprocess.CompletedProcess(cmd, 0, stdout="success\n", stderr="")

    monkeypatch.setattr(verify.IsolatedDatabaseManager, "start", mock_start)
    monkeypatch.setattr(verify.IsolatedDatabaseManager, "stop", mock_stop)
    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)
    monkeypatch.setattr(verify, "get_git_sha", lambda: "abcdef1234567890abcdef1234567890abcdef12")

    code = verify.execute_gate(
        "race-lab", None, central, fragments, tmp_path, "secret_check", fresh=True
    )
    assert code == 0

    assert len(captured_tokens) > 0, "Expected approval token to be generated and passed"
    raw_token = captured_tokens[0]

    # Inspect approval.json in evidence directory: must be hash-only
    app_file = tmp_path / "approval.json"
    assert app_file.is_file(), "approval.json must be recorded in evidence"
    app_data = json.loads(app_file.read_text(encoding="utf-8"))
    assert "token" not in app_data, "Raw token must not be in approval.json"
    assert "token_sha256" in app_data, "token_sha256 must be present in approval.json"
    import hashlib

    assert app_data["token_sha256"] == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    # Inspect manifest.json: commands must be sanitized
    manifest_file = tmp_path / "manifest.json"
    assert manifest_file.is_file()
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    for entry in manifest_data["commands"]:
        cmd_str = " ".join(entry["cmd"])
        assert raw_token not in cmd_str, f"Raw token leaked in manifest command: {cmd_str}"
        assert secret_pw not in cmd_str, f"DB password leaked in manifest command: {cmd_str}"

    # Search ALL files in evidence directory: assert raw token and DB password are completely absent
    for f in tmp_path.rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="ignore")
            assert raw_token not in text, f"Raw approval token found in {f.name}"
            assert secret_pw not in text, f"Database password found in {f.name}"


def test_isolated_database_manager_nested_lifecycle_and_cleanup(tmp_path: Path) -> None:
    """Test that nested IsolatedDatabaseManager instances are properly tracked and cleaned up."""
    mgr1 = verify.IsolatedDatabaseManager(tmp_path, "run1")
    mgr2 = verify.IsolatedDatabaseManager(tmp_path, "run2")

    assert mgr1.project_name != mgr2.project_name

    # Test context manager interface
    with verify.IsolatedDatabaseManager(tmp_path, "ctx_run") as mgr_ctx:
        assert mgr_ctx.project_name.startswith("cb_test_")

    # Verify stop() idempotency and active managers tracking
    mgr1._started = True
    verify.IsolatedDatabaseManager._active_managers.add(mgr1)
    mgr2._started = True
    verify.IsolatedDatabaseManager._active_managers.add(mgr2)

    assert mgr1 in verify.IsolatedDatabaseManager._active_managers
    assert mgr2 in verify.IsolatedDatabaseManager._active_managers

    mgr1.stop()
    assert mgr1 not in verify.IsolatedDatabaseManager._active_managers

    verify.IsolatedDatabaseManager._cleanup_all()
    assert len(verify.IsolatedDatabaseManager._active_managers) == 0


def test_isolated_database_manager_stop_lifecycle_retains_active_state_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that stop() does not mark manager as stopped or discard it from active managers
    if compose down fails. The manager must remain retryable by _cleanup_all."""
    import subprocess

    mgr = verify.IsolatedDatabaseManager(tmp_path, "fail_test")
    mgr._started = True
    verify.IsolatedDatabaseManager._active_managers.add(mgr)

    # Simulate compose down failure
    failing_runs = [subprocess.CompletedProcess([], 1, stdout="", stderr="compose down error")]
    successful_runs = [subprocess.CompletedProcess([], 0, stdout="success", stderr="")]

    def mock_run_fail(*args, **kwargs):
        return failing_runs[0]

    monkeypatch.setattr(subprocess, "run", mock_run_fail)

    with pytest.raises(RuntimeError, match="Database cleanup failed"):
        mgr.stop()

    # Manager must NOT be marked stopped and must STILL be in active managers
    assert not mgr._stopped
    assert mgr in verify.IsolatedDatabaseManager._active_managers

    # Now simulate recovery on retry (e.g. during _cleanup_all)
    def mock_run_succeed(*args, **kwargs):
        return successful_runs[0]

    monkeypatch.setattr(subprocess, "run", mock_run_succeed)

    # Retrying stop() should now succeed and mark manager stopped
    verify.IsolatedDatabaseManager._cleanup_all()
    assert mgr._stopped
    assert mgr not in verify.IsolatedDatabaseManager._active_managers


def test_run_command_output_and_exception_sanitization_without_mocking(tmp_path: Path) -> None:
    """Real execution test: run_command executes without mocking and sanitizes secrets in output."""
    secret_pass = "super_secret_db_pass_12345"
    raw_url = f"postgresql://commonsbook_test:{secret_pass}@127.0.0.1:5432/testdb"
    env = {
        "CLEANUP_VERIFY_DSN": raw_url,
        "SECRET_AUTH_TOKEN": "token_abc_xyz_secret_9999",
    }

    # 1. Real execution where child echoes raw secret to stdout and stderr
    code = verify.run_command(
        [
            sys.executable,
            "-c",
            (
                "import os, sys\n"
                "print(os.environ.get('CLEANUP_VERIFY_DSN'))\n"
                "print('error: ' + os.environ.get('SECRET_AUTH_TOKEN'), file=sys.stderr)\n"
            ),
        ],
        tmp_path,
        1,
        env=env,
    )
    assert code == 0
    stdout_txt = (tmp_path / "cmd_01_stdout.txt").read_text(encoding="utf-8")
    stderr_txt = (tmp_path / "cmd_01_stderr.txt").read_text(encoding="utf-8")

    assert secret_pass not in stdout_txt
    assert "token_abc_xyz_secret_9999" not in stderr_txt
    assert "postgresql://commonsbook_test:***@127.0.0.1:5432/testdb" in stdout_txt
    assert "[REDACTED]" in stderr_txt

    # 2. Real execution failure: generic failure message must not leak command or secrets
    code_err = verify.run_command(
        ["nonexistent_binary_xyz_12345", "--token", "secret_arg_token"],
        tmp_path,
        2,
        env=env,
    )
    assert code_err != 0
    err_txt = (tmp_path / "cmd_02_stderr.txt").read_text(encoding="utf-8")
    assert "secret_arg_token" not in err_txt
    assert "Command execution failed" in err_txt


def test_cleanup_verification_script_and_sanitized_command_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cleanup verification script runs against real python, handles failures cleanly
    without traceback credentials, and records the actual sanitized command."""
    secret_pw = "leak_check_password_888"
    fake_admin_url = f"postgresql://commonsbook_test:{secret_pw}@127.0.0.1:65534/postgres"
    fake_db_name = "commonsbook_racelab_test123"

    runner_code = (SCRIPTS_DIR / "verify.py").read_text(encoding="utf-8")
    assert "CLEANUP_VERIFY_DSN" in runner_code
    assert "verify_cleanup_cmd = [" in runner_code

    # Real python execution of the cleanup check script against an unreachable port
    script = (
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
    cmd = [sys.executable, "-c", script, fake_db_name]
    env = {**os.environ, "CLEANUP_VERIFY_DSN": fake_admin_url}

    clean_code = verify.run_command(cmd, tmp_path, 1, env=env)
    assert clean_code != 0

    stderr_txt = (tmp_path / "cmd_01_stderr.txt").read_text(encoding="utf-8")
    assert secret_pw not in stderr_txt
    assert "Database connection failed during cleanup verification" in stderr_txt
    assert "Traceback" not in stderr_txt

    sanitized_cmd = verify.sanitize_command(cmd)
    cmd_str = " ".join(sanitized_cmd)
    assert secret_pw not in cmd_str
    assert fake_db_name in cmd_str


def test_scan_evidence_for_leaks_detects_unmasked_credentials(tmp_path: Path) -> None:
    """Test that scan_evidence_for_leaks identifies unmasked passwords and known secrets."""
    clean_file = tmp_path / "clean_evidence.txt"
    clean_file.write_text(
        "Connecting to postgresql://commonsbook_test:***@127.0.0.1:5432/db", encoding="utf-8"
    )

    violations_clean = verify.scan_evidence_for_leaks(tmp_path)
    assert len(violations_clean) == 0

    dirty_file = tmp_path / "dirty_evidence.txt"
    dirty_file.write_text(
        "Connecting to postgresql://commonsbook_test:raw_unmasked_pass@127.0.0.1:5432/db",
        encoding="utf-8",
    )

    violations_dirty = verify.scan_evidence_for_leaks(tmp_path)
    assert len(violations_dirty) == 1
    assert "dirty_evidence.txt" in violations_dirty[0]

    # Test known secrets detection
    secret_file = tmp_path / "secret_evidence.txt"
    secret_file.write_text("raw_token_xyz_987654321", encoding="utf-8")
    violations_with_secret = verify.scan_evidence_for_leaks(
        tmp_path, known_secrets=["raw_token_xyz_987654321"]
    )
    assert any("secret_evidence.txt" in v for v in violations_with_secret)
