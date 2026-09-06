import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verify  # noqa: E402


def test_unimplemented_target_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["verify.py", "concurrency"])
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
