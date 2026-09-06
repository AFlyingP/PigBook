import json
import os
import stat
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verification_cache  # noqa: E402


def setup_test_tree(root: Path) -> None:
    (root / "backend" / "app").mkdir(parents=True, exist_ok=True)
    (root / "backend" / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "verification.d").mkdir(parents=True, exist_ok=True)
    (root / "infra").mkdir(parents=True, exist_ok=True)
    (root / "frontend").mkdir(parents=True, exist_ok=True)

    (root / "backend" / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (root / "backend" / "uv.lock").write_text("lock-v1\n", encoding="utf-8")
    (root / "frontend" / "package.json").write_text('{"name": "test"}\n', encoding="utf-8")
    (root / "frontend" / "package-lock.json").write_text(
        '{"lockfileVersion": 3}\n', encoding="utf-8"
    )
    (root / "scripts" / "verification.json").write_text(
        '{"ordered_targets": []}\n', encoding="utf-8"
    )
    (root / "backend" / "app" / "main.py").write_text("def app(): pass\n", encoding="utf-8")
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (root / "compose.test.yaml").write_text("services: {}\n", encoding="utf-8")


def test_identical_input_trees_produce_identical_fingerprint(tmp_path: Path) -> None:
    tree1 = tmp_path / "tree1"
    tree2 = tmp_path / "tree2"
    setup_test_tree(tree1)
    setup_test_tree(tree2)

    fp1 = verification_cache.compute_fingerprint("concurrency", root_dir=tree1)
    fp2 = verification_cache.compute_fingerprint("concurrency", root_dir=tree2)
    assert fp1 == fp2


def test_one_byte_content_change_changes_fingerprint(tmp_path: Path) -> None:
    setup_test_tree(tmp_path)
    fp1 = verification_cache.compute_fingerprint("concurrency", root_dir=tmp_path)

    # Change one byte
    main_file = tmp_path / "backend" / "app" / "main.py"
    with open(main_file, "ab") as f:
        f.write(b"X")

    fp2 = verification_cache.compute_fingerprint("concurrency", root_dir=tmp_path)
    assert fp1 != fp2


def test_file_mode_change_changes_fingerprint(tmp_path: Path) -> None:
    setup_test_tree(tmp_path)
    target = tmp_path / "backend" / "app" / "main.py"
    fp1 = verification_cache.compute_fingerprint("concurrency", root_dir=tmp_path)

    # Toggle to read-only
    os.chmod(target, stat.S_IREAD)
    try:
        fp2 = verification_cache.compute_fingerprint("concurrency", root_dir=tmp_path)
        assert fp1 != fp2
    finally:
        os.chmod(target, stat.S_IWRITE)


def test_reordering_directory_traversal_does_not_change_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_test_tree(tmp_path)
    fp1 = verification_cache.compute_fingerprint("concurrency", root_dir=tmp_path)

    # Even if file discovery order were reversed, get_gate_inputs sorts by relative path
    orig_get_inputs = verification_cache.get_gate_inputs

    def reverse_order_inputs(gate: str, root: Path) -> list[Path]:
        res = orig_get_inputs(gate, root)
        return list(reversed(res))

    monkeypatch.setattr(verification_cache, "get_gate_inputs", lambda g, r: orig_get_inputs(g, r))
    fp2 = verification_cache.compute_fingerprint("concurrency", root_dir=tmp_path)
    assert fp1 == fp2


def test_reuse_refused_when_source_run_failed(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence" / "run_01"
    evidence_dir.mkdir(parents=True)
    manifest = {
        "target": "integration",
        "exit_code": 1,
        "input_fingerprint": "hash123",
        "tool_versions": {"uv": "1.0"},
        "test_profile": "standard",
    }
    (evidence_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    eligible, reason, match = verification_cache.check_reuse_eligibility(
        gate="integration",
        candidate_fingerprint="hash123",
        candidate_tools={"uv": "1.0"},
        test_profile="standard",
        evidence_root=tmp_path / "evidence",
        fresh=False,
    )
    assert not eligible
    assert "failed" in reason.lower()
    assert match is None


def test_reuse_refused_when_fingerprint_differs(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence" / "run_01"
    evidence_dir.mkdir(parents=True)
    manifest = {
        "target": "integration",
        "exit_code": 0,
        "input_fingerprint": "hash_source",
        "tool_versions": {"uv": "1.0"},
        "test_profile": "standard",
    }
    (evidence_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    eligible, reason, match = verification_cache.check_reuse_eligibility(
        gate="integration",
        candidate_fingerprint="hash_different",
        candidate_tools={"uv": "1.0"},
        test_profile="standard",
        evidence_root=tmp_path / "evidence",
        fresh=False,
    )
    assert not eligible
    assert "mismatch" in reason.lower()
    assert match is None


def test_reused_manifest_records_reused_true_and_original_sha(tmp_path: Path) -> None:
    source_dir = tmp_path / "evidence" / "source_run"
    source_dir.mkdir(parents=True)
    source_manifest_path = source_dir / "manifest.json"
    source_manifest = {
        "target": "integration",
        "exit_code": 0,
        "git_sha": "source_git_sha_original",
        "input_fingerprint": "hash123",
        "tool_versions": {"uv": "1.0"},
        "test_profile": "standard",
        "commands": [{"cmd": ["pytest"], "exit_code": 0}],
    }
    source_manifest_path.write_text(json.dumps(source_manifest), encoding="utf-8")

    reused = verification_cache.create_reused_manifest(
        gate="integration",
        source_manifest=source_manifest,
        source_manifest_path=source_manifest_path,
        candidate_sha="candidate_git_sha_current",
        candidate_fingerprint="hash123",
        run_id="test_run_reuse",
        timestamp="2026-09-06T12:00:00Z",
    )

    assert reused["reused"] is True
    assert reused["execution_type"] == "reused"
    assert reused["label"] == "reused, never rerun"
    assert reused["source_verified_sha"] == "source_git_sha_original"
    assert reused["candidate_sha"] == "candidate_git_sha_current"
    assert reused["source_manifest_hash"] is not None


def test_fresh_disables_reuse_even_when_fingerprint_matches(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence" / "run_01"
    evidence_dir.mkdir(parents=True)
    manifest = {
        "target": "integration",
        "exit_code": 0,
        "input_fingerprint": "hash123",
        "tool_versions": {"uv": "1.0"},
        "test_profile": "standard",
    }
    (evidence_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    eligible, reason, match = verification_cache.check_reuse_eligibility(
        gate="integration",
        candidate_fingerprint="hash123",
        candidate_tools={"uv": "1.0"},
        test_profile="standard",
        evidence_root=tmp_path / "evidence",
        fresh=True,
    )
    assert not eligible
    assert "fresh" in reason.lower()
    assert match is None


def test_timestamps_do_not_participate_in_fingerprint(tmp_path: Path) -> None:
    setup_test_tree(tmp_path)
    fp1 = verification_cache.compute_fingerprint("concurrency", root_dir=tmp_path)

    # Touch mtime of a file without changing content
    target = tmp_path / "backend" / "app" / "main.py"
    os.utime(target, (100000000, 100000000))

    fp2 = verification_cache.compute_fingerprint("concurrency", root_dir=tmp_path)
    assert fp1 == fp2


def test_invocation_with_matching_fingerprint_executes_when_reuse_not_activated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import verify

    manifest_path = SCRIPTS_DIR / "verification.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_data.get("evidence_reuse", {}).get("activated") is False

    target = "lint"
    candidate_fp = verification_cache.compute_fingerprint(target)

    # Prior successful run in evidence root
    evidence_root = tmp_path / "prior_evidence"
    prior_run_dir = evidence_root / "run_prior"
    prior_run_dir.mkdir(parents=True)
    prior_manifest = {
        "target": target,
        "exit_code": 0,
        "input_fingerprint": candidate_fp,
        "tool_versions": verify.get_tool_versions(),
        "test_profile": "standard",
    }
    (prior_run_dir / "manifest.json").write_text(json.dumps(prior_manifest), encoding="utf-8")

    current_evidence_dir = tmp_path / "current_evidence"
    monkeypatch.setenv("EVIDENCE_ROOT", str(evidence_root))
    monkeypatch.setenv("EVIDENCE_DIR", str(current_evidence_dir))

    executed: list[str] = []

    def mock_run_target(
        tgt, ticket, central_manifest, fragments, evidence_dir, command_log, run_id
    ):
        executed.append(tgt)
        return 0

    monkeypatch.setattr(verify, "run_target", mock_run_target)
    monkeypatch.setattr(sys, "argv", ["verify.py", target])

    verify.main()

    # The runner must execute target rather than taking reuse path
    assert executed == [target], "Runner should have executed target instead of taking reuse path"

    manifest_text = (current_evidence_dir / "manifest.json").read_text(encoding="utf-8")
    output_manifest = json.loads(manifest_text)
    assert output_manifest["reused"] is False
    assert output_manifest["execution_type"] == "fresh"

    captured = capsys.readouterr().out
    assert "[CACHE] Reusing manifest" not in captured
    assert "(reused)" not in captured


def test_reuse_eligibility_helper_reports_fingerprint_match_directly(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    run_dir = evidence_root / "successful_run"
    run_dir.mkdir(parents=True)

    test_fingerprint = "deterministic_sha256_hash_abc123"
    test_tools = {"uv": "0.12.10", "node": "22"}
    test_profile = "standard"

    prior_manifest = {
        "target": "concurrency",
        "exit_code": 0,
        "input_fingerprint": test_fingerprint,
        "tool_versions": test_tools,
        "test_profile": test_profile,
    }
    manifest_file = run_dir / "manifest.json"
    manifest_file.write_text(json.dumps(prior_manifest), encoding="utf-8")

    eligible, reason, match = verification_cache.check_reuse_eligibility(
        gate="concurrency",
        candidate_fingerprint=test_fingerprint,
        candidate_tools=test_tools,
        test_profile=test_profile,
        evidence_root=evidence_root,
        fresh=False,
    )

    assert eligible is True
    assert "Valid matching execution evidence found" in reason
    assert match is not None
    matched_data, matched_path = match
    assert matched_data["target"] == "concurrency"
    assert matched_data["input_fingerprint"] == test_fingerprint
    assert matched_path == manifest_file


def test_reusable_gates_set_matches_manifest_and_excludes_standard_gates() -> None:
    manifest_path = SCRIPTS_DIR / "verification.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    reuse_config = manifest_data.get("evidence_reuse", {})

    assert reuse_config.get("activated") is False
    assert reuse_config.get("activation_target") == "T-011"

    reusable_gates = reuse_config.get("reusable_gates", [])
    assert set(reusable_gates) == {"concurrency", "permissions"}

    # Must NOT include lint, unit, or integration
    assert "lint" not in reusable_gates
    assert "unit" not in reusable_gates
    assert "integration" not in reusable_gates

    # Verification cache helper functions reflect manifest configuration
    expected_gates = {"concurrency", "permissions"}
    assert set(verification_cache.get_reusable_gates(manifest_data)) == expected_gates
    assert set(verification_cache.get_reusable_gates()) == expected_gates
    assert verification_cache.is_evidence_reuse_activated(manifest_data) is False
    assert verification_cache.is_evidence_reuse_activated() is False
