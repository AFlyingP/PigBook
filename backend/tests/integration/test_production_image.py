import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def is_docker_available() -> bool:
    try:
        res = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        return res.returncode == 0
    except Exception:
        return False


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def test_production_image_build_and_behavior() -> None:
    """Build single linux/amd64 production image and verify executable contracts (Spec 9.1)."""
    if not is_docker_available():
        pytest.skip("Docker daemon unavailable in execution environment")

    image_tag = f"commonsbook-prod-test:{uuid.uuid4().hex[:8]}"

    # 1. Build production multi-stage image
    build_cmd = [
        "docker",
        "build",
        "-t",
        image_tag,
        "-f",
        "infra/Dockerfile.api",
        ".",
    ]
    build_res = subprocess.run(
        build_cmd,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert build_res.returncode == 0, f"Docker build failed: {build_res.stderr}"

    try:
        # 2. Assert nonroot execution: container default UID must not be 0 (root)
        id_res = subprocess.run(
            ["docker", "run", "--rm", image_tag, "id", "-u"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert id_res.returncode == 0
        uid = id_res.stdout.strip()
        assert uid != "0", f"Container must run as nonroot user, got UID={uid}"
        assert uid == "10001"

        # 3. Assert production rejection of insecure configuration (R5)
        valid_key = "a" * 32
        insecure_res = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-e",
                "APP_ENV=production",
                "-e",
                "JWT_SECRET=",
                image_tag,
                "python",
                "-c",
                "from app.config import get_settings; get_settings()",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert insecure_res.returncode != 0
        assert "JWT_SECRET must be at least 32 bytes in production" in insecure_res.stderr

        # Missing SENTRY_DSN in production must fail
        insecure_sentry = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-e",
                "APP_ENV=production",
                "-e",
                f"JWT_SECRET={valid_key}",
                "-e",
                f"RATE_LIMIT_HMAC_SECRET={valid_key}",
                "-e",
                f"METRICS_TOKEN={valid_key}",
                "-e",
                "SENTRY_DSN=",
                image_tag,
                "python",
                "-c",
                "from app.config import get_settings; get_settings()",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert insecure_sentry.returncode != 0
        assert "SENTRY_DSN is required in production" in insecure_sentry.stderr

        # Valid production configuration passes startup validation
        valid_prod = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-e",
                "APP_ENV=production",
                "-e",
                f"JWT_SECRET={valid_key}",
                "-e",
                f"RATE_LIMIT_HMAC_SECRET={valid_key}",
                "-e",
                f"METRICS_TOKEN={valid_key}",
                "-e",
                "SENTRY_DSN=https://example@sentry.invalid/1",
                image_tag,
                "python",
                "-c",
                "import app.config; assert app.config.get_settings().APP_ENV == 'production'",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert valid_prod.returncode == 0, f"Valid production config failed: {valid_prod.stderr}"

        # 4. Run container and assert HTTP endpoints, static serving, and fallbacks
        port = find_free_port()
        container_name = f"cb_prod_test_{uuid.uuid4().hex[:8]}"
        run_cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            f"{port}:10000",
            "-e",
            "APP_ENV=local",
            "-e",
            "PORT=10000",
            image_tag,
        ]
        start_res = subprocess.run(
            run_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert start_res.returncode == 0, f"Failed to start container: {start_res.stderr}"

        try:
            # Wait for container Uvicorn process to bind PORT and become responsive
            base_url = f"http://127.0.0.1:{port}"
            server_ready = False
            for _ in range(30):
                try:
                    with urllib.request.urlopen(f"{base_url}/healthz", timeout=1) as resp:
                        if resp.status == 200:
                            server_ready = True
                            break
                except Exception:
                    time.sleep(0.5)

            assert server_ready, "Production container failed to respond on /healthz within timeout"

            # 4a. Verify /healthz returns 200 ok
            with urllib.request.urlopen(f"{base_url}/healthz") as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["status"] == "ok"

            # 4b. Verify /readyz without database returns 503 with standard error envelope
            try:
                urllib.request.urlopen(f"{base_url}/readyz")
                pytest.fail("/readyz without DB must return 503")
            except urllib.error.HTTPError as err:
                assert err.code == 503
                assert err.headers.get("Retry-After") == "1"
                err_data = json.loads(err.read().decode("utf-8"))
                assert err_data["error"]["code"] == "RETRYABLE_UNAVAILABLE"
                assert err_data["error"]["message"] == "Service unavailable"

            # 4c. Verify compiled frontend static serving on root /
            with urllib.request.urlopen(f"{base_url}/") as resp:
                assert resp.status == 200
                assert "text/html" in resp.headers.get("Content-Type", "")
                body = resp.read().decode("utf-8")
                assert '<div id="root">' in body

            # 4d. Verify client-side deep-link fallback returns index.html
            with urllib.request.urlopen(f"{base_url}/resources/test-deep-link") as resp:
                assert resp.status == 200
                assert "text/html" in resp.headers.get("Content-Type", "")
                body = resp.read().decode("utf-8")
                assert '<div id="root">' in body

            # 4e. Verify unknown API routes return JSON 404 envelope, never index.html
            try:
                urllib.request.urlopen(f"{base_url}/api/v1/unknown-route-that-does-not-exist")
                pytest.fail("Unknown API route must return 404")
            except urllib.error.HTTPError as err:
                assert err.code == 404
                assert "application/json" in err.headers.get("Content-Type", "")
                err_data = json.loads(err.read().decode("utf-8"))
                assert err_data["error"]["code"] == "NOT_FOUND"
                assert err_data["error"]["message"] == "Not found"

            # 4f. Path traversal regression: must never serve files outside dist_dir (R1)
            for traversal_path in [
                "/../../backend/pyproject.toml",
                "/..%2F..%2Fbackend%2Fpyproject.toml",
                "/assets/../../backend/pyproject.toml",
                "/../../../etc/passwd",
                "/.env",
            ]:
                try:
                    with urllib.request.urlopen(f"{base_url}{traversal_path}") as resp:
                        content = resp.read().decode("utf-8", errors="ignore")
                        assert "tool.poetry" not in content
                        assert "project.dependencies" not in content
                        assert "root:" not in content
                except urllib.error.HTTPError as err:
                    assert err.code == 404
                    err_content = err.read().decode("utf-8", errors="ignore")
                    assert "pyproject.toml" not in err_content

        finally:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

    finally:
        subprocess.run(
            ["docker", "rmi", "-f", image_tag], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
