"""Shared test configuration.

R0 gate awareness (.claude/rules/project-rules.json): when EOE_GATE=1, any
skipped, xfailed, or deselected test is a gate violation. Enforcement (the
nonzero exit) lives in tests/gate_runner.py, which the gate scripts invoke,
because pytest 9 ignores exitstatus mutation in this hook (DECISIONS D11).
This hook prints the violation loudly so plain `pytest` runs surface it too.
Filtered runs stay legal while debugging between gates; they are only
forbidden as a way to clear a gate.
"""

import base64
import contextlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from hypothesis import settings as hypothesis_settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# E2.3: property-based cases in the test-critical merge suite run
# DETERMINISTICALLY - a gate must never be red or green by luck (rule R0).
# derandomize fixes the example stream; no deadline because gate machines
# vary (CI shares cores with the compose builds).
hypothesis_settings.register_profile("gate", derandomize=True, deadline=None)
hypothesis_settings.load_profile("gate")


def make_kek() -> str:
    """A structurally valid platform KEK (base64 of 32 random bytes) for test
    Settings; EOE_KEK is validated at app construction (E0.11)."""
    return base64.b64encode(os.urandom(32)).decode()


def docker_cli() -> str:
    """Locate docker, tolerating a PATH captured before Docker Desktop installed."""
    found = shutil.which("docker")
    if found:
        return found
    fallback = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    if os.path.exists(fallback):
        return fallback
    raise AssertionError("docker not found; Docker is a hard gate prerequisite (rule R0)")


def docker_env() -> dict[str, str]:
    """Process env with the docker CLI's directory appended to PATH (D12)."""
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.dirname(docker_cli())
    return env


@contextlib.contextmanager
def ephemeral_postgres(migrate: bool = True):
    """Disposable Postgres on a unique container name and a Docker-assigned
    free host port, so any number of test modules can hold one without
    colliding with each other or with orphans from interrupted runs."""
    name = f"eoe-pg-{uuid.uuid4().hex[:10]}"
    secret = uuid.uuid4().hex
    env = docker_env()
    run = subprocess.run(
        [
            docker_cli(),
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1:0:5432",
            "-e",
            f"POSTGRES_PASSWORD={secret}",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert run.returncode == 0, f"could not start ephemeral postgres: {run.stderr}"
    try:
        streak = 0
        for _ in range(90):
            probe = subprocess.run(
                [docker_cli(), "exec", name, "pg_isready", "-U", "postgres"],
                capture_output=True,
                env=env,
            )
            streak = streak + 1 if probe.returncode == 0 else 0
            if streak >= 2:  # survives the init-time restart
                break
            time.sleep(1)
        else:
            raise AssertionError("ephemeral postgres never became ready")
        ports = subprocess.run(
            [docker_cli(), "port", name, "5432/tcp"], capture_output=True, text=True, env=env
        )
        assert ports.returncode == 0, ports.stderr
        host_port = ports.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        url = f"postgresql+psycopg://postgres:{secret}@127.0.0.1:{host_port}/postgres"
        if migrate:
            upgraded = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=REPO_ROOT / "backend",
                capture_output=True,
                text=True,
                env={**env, "DATABASE_URL": url},
                timeout=120,
            )
            assert upgraded.returncode == 0, f"migration failed: {upgraded.stderr}"
        yield url
    finally:
        subprocess.run([docker_cli(), "rm", "-f", "-v", name], capture_output=True, env=env)


def run_git(*args: str) -> str:
    """Run git in the repo root, decoding output as UTF-8 explicitly.

    Windows would otherwise decode subprocess output with the ANSI code page
    (cp1252), which breaks on the spec's UTF-8 diagrams (see DECISIONS D10).
    """
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("EOE_GATE") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    counts = {key: len(reporter.stats.get(key, [])) for key in ("skipped", "xfailed", "deselected")}
    if any(counts.values()):
        reporter.write_line(
            f"EOE_GATE violation (rule R0): {counts}. "
            "Skipped, xfailed, and deselected tests are hard failures at a gate; "
            "tests/gate_runner.py turns this into a failing exit.",
            red=True,
        )
