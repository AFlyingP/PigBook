from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def parse_conservative_yaml(content: str) -> dict[str, Any]:
    """
    Conservative standard-library YAML parser for GitHub Actions workflows.
    Parses nested mappings, sequences, scalar types, and multiline block scalars ('|').
    """
    lines = content.splitlines()

    def get_indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def clean_val(raw: str) -> Any:
        v = raw.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            return v[1:-1]
        if v.lower() == "true":
            return True
        if v.lower() == "false":
            return False
        if v.isdigit():
            return int(v)
        return v

    def parse_block(idx: int, min_indent: int) -> tuple[Any, int]:
        while idx < len(lines):
            line = lines[idx]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                idx += 1
                continue
            indent = get_indent(line)
            if indent < min_indent:
                return None, idx
            if stripped.startswith("- "):
                return parse_list(idx, indent)
            else:
                return parse_map(idx, indent)
        return {}, idx

    def parse_map(idx: int, block_indent: int) -> tuple[dict[str, Any], int]:
        res: dict[str, Any] = {}
        while idx < len(lines):
            line = lines[idx]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                idx += 1
                continue
            indent = get_indent(line)
            if indent < block_indent:
                break
            if ":" not in stripped:
                idx += 1
                continue

            k, v = stripped.split(":", 1)
            k = k.strip()
            v = v.strip()

            if v == "|":
                idx += 1
                multiline_indent = None
                multiline_lines: list[str] = []
                while idx < len(lines):
                    m_line = lines[idx]
                    m_stripped = m_line.strip()
                    if not m_stripped:
                        multiline_lines.append("")
                        idx += 1
                        continue
                    m_indent = get_indent(m_line)
                    if multiline_indent is None:
                        if m_indent <= block_indent:
                            break
                        multiline_indent = m_indent
                    elif m_indent < multiline_indent:
                        break
                    multiline_lines.append(m_line[multiline_indent:])
                    idx += 1
                res[k] = "\n".join(multiline_lines).strip()
            elif v == "":
                idx += 1
                child_val, idx = parse_block(idx, block_indent + 1)
                res[k] = child_val
            else:
                res[k] = clean_val(v)
                idx += 1
        return res, idx

    def parse_list(idx: int, block_indent: int) -> tuple[list[Any], int]:
        res: list[Any] = []
        while idx < len(lines):
            line = lines[idx]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                idx += 1
                continue
            indent = get_indent(line)
            if indent < block_indent:
                break
            if not stripped.startswith("- "):
                break

            item_str = stripped[2:].strip()
            if ":" in item_str and not (item_str.startswith('"') or item_str.startswith("'")):
                k, v = item_str.split(":", 1)
                k = k.strip()
                v = v.strip()
                item_map: dict[str, Any] = {}
                res.append(item_map)

                item_map_indent = indent + 2
                if v == "|":
                    idx += 1
                    multiline_indent = None
                    multiline_lines = []
                    while idx < len(lines):
                        m_line = lines[idx]
                        m_stripped = m_line.strip()
                        if not m_stripped:
                            multiline_lines.append("")
                            idx += 1
                            continue
                        m_indent = get_indent(m_line)
                        if multiline_indent is None:
                            if m_indent <= indent:
                                break
                            multiline_indent = m_indent
                        elif m_indent < multiline_indent:
                            break
                        multiline_lines.append(m_line[multiline_indent:])
                        idx += 1
                    item_map[k] = "\n".join(multiline_lines).strip()
                elif v == "":
                    idx += 1
                    child_val, idx = parse_block(idx, item_map_indent)
                    item_map[k] = child_val
                else:
                    item_map[k] = clean_val(v)
                    idx += 1

                while idx < len(lines):
                    sub_line = lines[idx]
                    sub_stripped = sub_line.strip()
                    if not sub_stripped or sub_stripped.startswith("#"):
                        idx += 1
                        continue
                    sub_indent = get_indent(sub_line)
                    if sub_indent <= indent:
                        break
                    if (
                        sub_indent == item_map_indent
                        and ":" in sub_stripped
                        and not sub_stripped.startswith("- ")
                    ):
                        sub_k, sub_v = sub_stripped.split(":", 1)
                        sub_k = sub_k.strip()
                        sub_v = sub_v.strip()
                        if sub_v == "|":
                            idx += 1
                            multiline_indent = None
                            multiline_lines = []
                            while idx < len(lines):
                                m_line = lines[idx]
                                m_stripped = m_line.strip()
                                if not m_stripped:
                                    multiline_lines.append("")
                                    idx += 1
                                    continue
                                m_indent = get_indent(m_line)
                                if multiline_indent is None:
                                    if m_indent <= sub_indent:
                                        break
                                    multiline_indent = m_indent
                                elif m_indent < multiline_indent:
                                    break
                                multiline_lines.append(m_line[multiline_indent:])
                                idx += 1
                            item_map[sub_k] = "\n".join(multiline_lines).strip()
                        elif sub_v == "":
                            idx += 1
                            sub_child, idx = parse_block(idx, item_map_indent + 1)
                            item_map[sub_k] = sub_child
                        else:
                            item_map[sub_k] = clean_val(sub_v)
                            idx += 1
                    else:
                        break
            else:
                res.append(clean_val(item_str))
                idx += 1
        return res, idx

    parsed, _ = parse_map(0, 0)
    return parsed


@pytest.fixture(scope="module")
def raw_ci_yaml() -> str:
    assert CI_WORKFLOW_PATH.exists(), f"Missing workflow file at {CI_WORKFLOW_PATH}"
    return CI_WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ci_workflow(raw_ci_yaml: str) -> dict[str, Any]:
    return parse_conservative_yaml(raw_ci_yaml)


def test_triggers(ci_workflow: dict[str, Any]) -> None:
    """1. Triggers include pull_request and push to main."""
    triggers = ci_workflow.get("on")
    assert isinstance(triggers, dict), "Workflow 'on' trigger configuration must be a mapping"
    assert "pull_request" in triggers, "Workflow must trigger on pull_request"
    assert "push" in triggers, "Workflow must trigger on push"

    push_config = triggers["push"]
    assert isinstance(push_config, dict), "push trigger must configure branch constraints"
    assert "branches" in push_config
    assert "main" in push_config["branches"], "push trigger must include 'main' branch"


def test_ci_bootstraps_once_and_runs_fresh_regression(ci_workflow: dict[str, Any]) -> None:
    jobs = ci_workflow["jobs"]
    assert list(jobs) == ["verify", "lint", "unit", "integration", "concurrency", "build"]
    expected = {
        "lint": "Lint and Typecheck",
        "unit": "Unit Tests",
        "integration": "Integration Tests",
        "concurrency": "Concurrency Gate",
        "build": "Build API Image",
    }
    for key, name in expected.items():
        job = jobs[key]
        assert job["name"] == name
        assert job["needs"] == "verify"
        assert job["if"] == "always()"
        assert job["steps"][0]["env"]["VERIFY_RESULT"] == "${{ needs.verify.result }}"
        assert job["steps"][0]["run"] == 'test "$VERIFY_RESULT" = "success"'

    scripts = "\n".join(step.get("run", "") for step in jobs["verify"]["steps"])
    assert scripts.count("python scripts/verify.py bootstrap") == 1
    assert "python scripts/verify.py regression --base" in scripts
    assert "python scripts/verify.py regression --fresh" in scripts
    assert '"build", "--fresh"' in scripts
    assert scripts.index("bootstrap") < scripts.index("regression") < scripts.index('"build"')


def test_permissions(ci_workflow: dict[str, Any], raw_ci_yaml: str) -> None:
    """3. Workflow default permissions are contents: read and no write scope appears anywhere."""
    workflow_perms = ci_workflow.get("permissions")
    assert workflow_perms == {"contents": "read"}, (
        "Default workflow permissions must be contents: read"
    )

    # Ensure no write scope exists in the entire workflow definition
    assert "write" not in raw_ci_yaml.lower(), "Forbidden write permission found in workflow YAML"

    for job_name, job in ci_workflow.get("jobs", {}).items():
        if "permissions" in job:
            job_perms = job["permissions"]
            if isinstance(job_perms, dict):
                for k, v in job_perms.items():
                    assert v != "write", f"Job {job_name} grants write permission to {k}"
            else:
                assert "write" not in str(job_perms).lower()


def test_concurrency_registration_is_honest() -> None:
    import json

    manifest = json.loads((CI_WORKFLOW_PATH.parents[2] / "scripts/verification.json").read_text())
    assert "concurrency" in manifest["ordered_targets"]
    assert "concurrency" not in manifest["implemented_targets"]
    runner = (CI_WORKFLOW_PATH.parents[2] / "scripts/verify.py").read_text()
    assert "not implemented; no passing evidence claimed" in runner


def test_cache_keys_strictly_lockfile_based(ci_workflow: dict[str, Any]) -> None:
    """5. Every cache key references uv.lock or package-lock.json; no caching of test results."""
    jobs = ci_workflow.get("jobs", {})
    cache_steps_found = 0
    forbidden_terms = ["evidence", "result", "manifest", "outcome", "pytest", "vitest"]

    for job_name, job in jobs.items():
        steps = job.get("steps", [])
        for step in steps:
            uses = step.get("uses", "")
            if "actions/cache" in uses:
                cache_steps_found += 1
                with_block = step.get("with", {})
                key = with_block.get("key", "")
                assert "backend/uv.lock" in key or "frontend/package-lock.json" in key, (
                    f"Job {job_name} cache key '{key}' does not reference a recognized lockfile"
                )

                for forbidden in forbidden_terms:
                    assert forbidden not in key.lower(), (
                        f"Job {job_name} cache key '{key}' contains forbidden term '{forbidden}'"
                    )

    assert cache_steps_found > 0, "Expected dependency caching steps to be configured"


def test_artifact_upload_uses_if_always(ci_workflow: dict[str, Any]) -> None:
    """6. Artifact upload uses if: always()."""
    jobs = ci_workflow.get("jobs", {})
    upload_steps_found = 0

    for job_name, job in jobs.items():
        steps = job.get("steps", [])
        for step in steps:
            uses = step.get("uses", "")
            if "actions/upload-artifact" in uses:
                upload_steps_found += 1
                assert step.get("if") == "always()", (
                    f"Job {job_name} artifact upload step must specify if: always()"
                )
                with_block = step.get("with", {})
                assert with_block.get("path") == "evidence/", (
                    f"Job {job_name} must upload evidence/ directory"
                )

    assert upload_steps_found == 1, "Shared execution must upload its evidence exactly once"


def test_no_deploy_or_publish_jobs(ci_workflow: dict[str, Any], raw_ci_yaml: str) -> None:
    """7. No deploy/publish/push job exists (absence of registry login, image push, or deploy)."""
    raw_lower = raw_ci_yaml.lower()
    forbidden_patterns = [
        "docker/login-action",
        "docker/build-push-action",
        "docker push",
        "deploy",
        "publish",
        "aws-actions",
        "google-github-actions",
        "azure/",
        "kubectl",
        "helm",
    ]

    for pattern in forbidden_patterns:
        assert pattern not in raw_lower, (
            f"Forbidden deploy/publish action or command '{pattern}' found in workflow"
        )

    jobs = ci_workflow.get("jobs", {})
    for job_name in jobs.keys():
        assert "deploy" not in job_name.lower(), f"Unexpected deploy job '{job_name}'"
        assert "publish" not in job_name.lower(), f"Unexpected publish job '{job_name}'"


def test_no_secrets_referenced(raw_ci_yaml: str) -> None:
    """8. No secrets are referenced in the workflow."""
    assert "secrets." not in raw_ci_yaml, "Workflow must not reference any secrets"


def test_stages_invoke_verify_runner(ci_workflow: dict[str, Any]) -> None:
    """9. The stages invoke scripts/verify.py rather than bypassing it."""
    jobs = ci_workflow.get("jobs", {})
    for job_name, job in jobs.items():
        if job_name != "verify":
            assert job["needs"] == "verify"
            continue
        steps = job.get("steps", [])
        run_steps = [s.get("run", "") for s in steps if "run" in s]

        invocations = [r for r in run_steps if "scripts/verify.py" in r]
        assert len(invocations) > 0, (
            f"Stage '{job_name}' must invoke scripts/verify.py to execute verification"
        )
