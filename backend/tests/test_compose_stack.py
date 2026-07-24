"""Gate 1: compose stack integration checks (task E0.1).

Exercises the real Docker stack. These tests are part of the gate and never
self-skip: Docker being unavailable is a failing gate, not a skipped test
(rule R0). Plan checks 9 through 11 run as one lifecycle test so up, probe,
and clean teardown assert against the same stack instance.
"""

import json
import subprocess
import urllib.request

import pytest
from conftest import REPO_ROOT
from test_repo_layout import compose_env, docker_cli, docker_env

DEPLOY = REPO_ROOT / "deploy"
PROJECT = "eoe-gate-test"

pytestmark = pytest.mark.integration


def _compose(*args: str, env: dict[str, str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [docker_cli(), "compose", "-f", str(DEPLOY / "docker-compose.yml"), "-p", PROJECT, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _http_status(url: str) -> int:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status


def test_stack_lifecycle_up_probe_teardown():
    env = compose_env()
    try:
        # Check 9: clean up, build, all four services healthy within the wait.
        up = _compose("up", "-d", "--build", "--wait", env=env)
        assert up.returncode == 0, f"compose up failed:\n{up.stdout}\n{up.stderr}"

        ps = _compose("ps", "--format", "json", env=env)
        assert ps.returncode == 0, ps.stderr
        services = [json.loads(line) for line in ps.stdout.splitlines() if line.strip()]
        states = {item["Service"]: item["State"] for item in services}
        assert states == {
            "api": "running",
            "frontend": "running",
            "postgres": "running",
            "redis": "running",
        }, f"unexpected service states: {states}"

        assert _http_status("http://localhost:8000/") == 200
        assert _http_status("http://localhost:5173/") == 200

        # Check 10: Postgres accepts the documented DATABASE_URL credential shape.
        ready = _compose("exec", "-T", "postgres", "pg_isready", "-U", "eoe", "-d", "eoe", env=env)
        assert ready.returncode == 0, f"pg_isready failed: {ready.stdout} {ready.stderr}"
    finally:
        down = _compose("down", "-v", "--remove-orphans", env=env)

    # Check 11: teardown leaves no containers or volumes behind.
    assert down.returncode == 0, f"compose down failed: {down.stderr}"
    leftover = _compose("ps", "-a", "--format", "json", env=env)
    assert leftover.stdout.strip() == "", f"orphan containers: {leftover.stdout}"
    volumes = subprocess.run(
        [
            docker_cli(),
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert volumes.stdout.strip() == "", f"orphan volumes: {volumes.stdout}"


def test_frontend_prod_image_builds():
    # Check 12 remainder: compose up already built the api image and the
    # frontend dev target; the nginx prod target must build too (D2).
    result = subprocess.run(
        [docker_cli(), "build", "--target", "prod", "-q", str(REPO_ROOT / "frontend")],
        capture_output=True,
        text=True,
        env=docker_env(),
        timeout=600,
    )
    assert result.returncode == 0, f"frontend prod build failed:\n{result.stderr}"
