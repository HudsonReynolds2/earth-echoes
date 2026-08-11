"""Gate 1: compose stack integration checks (task E0.1).

Exercises the real Docker stack. These tests are part of the gate and never
self-skip: Docker being unavailable is a failing gate, not a skipped test
(rule R0). Plan checks 9 through 11 run as one lifecycle test so up, probe,
and clean teardown assert against the same stack instance.
"""

import json
import subprocess
import urllib.error
import urllib.request

import pytest
from conftest import REPO_ROOT, bootstrap_broker_material
from test_repo_layout import COMPOSE_SERVICES, compose_env, docker_cli, docker_env

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


def _http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


@pytest.mark.timeout(1200)
def test_stack_lifecycle_up_probe_teardown():
    env = compose_env()
    bootstrap_broker_material()
    try:
        # Check 9: clean up, build, every service healthy within the wait.
        up = _compose("up", "-d", "--build", "--wait", env=env)
        assert up.returncode == 0, f"compose up failed:\n{up.stdout}\n{up.stderr}"

        ps = _compose("ps", "--format", "json", env=env)
        assert ps.returncode == 0, ps.stderr
        services = [json.loads(line) for line in ps.stdout.splitlines() if line.strip()]
        states = {item["Service"]: item["State"] for item in services}
        assert states == dict.fromkeys(COMPOSE_SERVICES, "running"), (
            f"unexpected service states: {states}"
        )

        health = _http_json("http://localhost:18000/api/v1/health")
        assert health["status"] == "ok"
        assert health["database"] == "ok", f"api cannot reach postgres: {health}"
        assert _http_status("http://localhost:15173/") == 200

        # The envelope is the only error shape, proven through the real stack.
        try:
            urllib.request.urlopen("http://localhost:18000/", timeout=10)
            raise AssertionError("root path should 404 under the /api/v1 prefix discipline")
        except urllib.error.HTTPError as error:
            assert error.code == 404
            body = json.loads(error.read().decode("utf-8"))
            assert body["error"]["code"] == "not_found"

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


@pytest.mark.timeout(1200)
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
