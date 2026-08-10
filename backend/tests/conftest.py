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
import dataclasses
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from hypothesis import settings as hypothesis_settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# E2.3: property-based cases in the test-critical merge suite run
# DETERMINISTICALLY - a gate must never be red or green by luck (rule R0).
# derandomize fixes the example stream; no deadline because gate machines
# vary (CI shares cores with the compose builds).
hypothesis_settings.register_profile("gate", derandomize=True, deadline=None)
hypothesis_settings.load_profile("gate")


@pytest.fixture
def anyio_backend() -> str:
    """Async tests (E3.2 onward) run on anyio's pytest plugin, which ships
    inside anyio — already a dependency through Starlette — rather than on a
    new pytest-asyncio dev dependency. Pinning the single backend keeps one
    test per test: parametrizing over trio too would double the suite for a
    runtime the app never uses.
    """
    return "asyncio"


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


def bootstrap_broker_material() -> None:
    """E3.1's first pass, for any suite that brings the compose stack up.

    Mosquitto refuses to start without its certificate, password and ACL files,
    and those are generated rather than committed — so every stack test
    provisions them exactly the way the README tells an operator to, before the
    first `compose up`.
    """
    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--certs-only"],
        cwd=REPO_ROOT / "backend",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"devbroker --certs-only failed:\n{result.stdout}\n{result.stderr}"
    )


def free_port() -> int:
    """A currently-free localhost port, for the cases that need a STABLE host
    port. Docker's `-p 127.0.0.1:0:8883` picks a fresh port on every container
    start, so a test that stops and restarts a broker (E3.2's reconnect
    acceptance) has to name the port itself or the client would be dialling
    the old one after the restart."""
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _wait_until_accepting(name: str, env: dict[str, str], attempts: int = 60) -> None:
    for _ in range(attempts):
        probe = subprocess.run(
            [docker_cli(), "exec", name, "nc", "-z", "127.0.0.1", "8883"],
            capture_output=True,
            env=env,
        )
        if probe.returncode == 0:
            return
        time.sleep(0.5)
    logs = subprocess.run([docker_cli(), "logs", name], capture_output=True, text=True, env=env)
    raise AssertionError(f"broker never accepted:\n{logs.stdout}\n{logs.stderr}")


@dataclasses.dataclass(frozen=True)
class Broker:
    """A running dev Mosquitto (E3.1). `port` is the host port for clients
    running in this process; `exec_client` runs mosquitto_sub or mosquitto_pub
    inside the container, which is how the ACL suite talks to the broker
    without depending on host TLS tooling."""

    name: str
    port: int
    dev_dir: Path

    def exec_client(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [docker_cli(), "exec", self.name, *args],
            capture_output=True,
            text=True,
            env=docker_env(),
            timeout=timeout,
        )

    def reload(self) -> None:
        """SIGHUP: Mosquitto re-reads passwd and acl without dropping retained
        state, which is how a devbroker re-run reaches a live broker."""
        subprocess.run(
            [docker_cli(), "kill", "-s", "HUP", self.name], capture_output=True, env=docker_env()
        )

    def stop(self) -> None:
        """Take the broker down the way an outage does — the container stops,
        every connection drops, and nothing tells the clients (E3.2)."""
        stopped = subprocess.run(
            [docker_cli(), "stop", "-t", "1", self.name],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        assert stopped.returncode == 0, f"could not stop the broker: {stopped.stderr}"

    def start(self) -> None:
        """Bring it back and wait until it accepts again. Only meaningful for
        a broker created on an explicit host port (see free_port)."""
        env = docker_env()
        started = subprocess.run(
            [docker_cli(), "start", self.name], capture_output=True, text=True, env=env
        )
        assert started.returncode == 0, f"could not restart the broker: {started.stderr}"
        _wait_until_accepting(self.name, env)


@contextlib.contextmanager
def ephemeral_broker(dev_dir: Path, host_port: int | None = None):
    """Disposable TLS Mosquitto carrying the material `app.devbroker` wrote
    into `dev_dir` (task E3.1).

    Files go in with `docker cp` rather than a bind mount ON PURPOSE: bind
    mounts of a WSL or Windows path through Docker Desktop translate
    differently per host, and the gate has to behave the same on all three.
    The generated files are already world-readable so the container's uid 1883
    can read them after the copy (see devbroker.write_artifacts).

    `host_port` defaults to a Docker-assigned one, which cannot collide with
    anything; pass an explicit port (see free_port) only when the test needs
    the mapping to survive a stop/start.
    """
    name = f"eoe-mqtt-{uuid.uuid4().hex[:10]}"
    env = docker_env()
    conf = REPO_ROOT / "deploy" / "mosquitto" / "mosquitto.conf"
    created = subprocess.run(
        [
            docker_cli(),
            "create",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{host_port or 0}:8883",
            "eclipse-mosquitto:2",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert created.returncode == 0, f"could not create broker container: {created.stderr}"
    try:
        copies = (
            (dev_dir, f"{name}:/mosquitto/dev"),
            (conf, f"{name}:/mosquitto/config/mosquitto.conf"),
        )
        for source, target in copies:
            copied = subprocess.run(
                [docker_cli(), "cp", str(source), target],
                capture_output=True,
                text=True,
                env=env,
            )
            assert copied.returncode == 0, f"docker cp {source} failed: {copied.stderr}"
        started = subprocess.run(
            [docker_cli(), "start", name], capture_output=True, text=True, env=env
        )
        assert started.returncode == 0, f"broker would not start: {started.stderr}"

        ports = subprocess.run(
            [docker_cli(), "port", name, "8883/tcp"], capture_output=True, text=True, env=env
        )
        if ports.returncode != 0:
            logs = subprocess.run(
                [docker_cli(), "logs", name], capture_output=True, text=True, env=env
            )
            raise AssertionError(f"broker exited: {ports.stderr}\n{logs.stdout}\n{logs.stderr}")
        published = int(ports.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
        _wait_until_accepting(name, env)
        yield Broker(name=name, port=published, dev_dir=dev_dir)
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
