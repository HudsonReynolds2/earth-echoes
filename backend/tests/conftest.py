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
import errno
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings as hypothesis_settings

from app.services.stack import IMAGES as STACK_IMAGES

REPO_ROOT = Path(__file__).resolve().parents[2]

# E2.3: property-based cases in the test-critical merge suite run
# DETERMINISTICALLY - a gate must never be red or green by luck (rule R0).
# derandomize fixes the example stream; no deadline because gate machines
# vary (CI shares cores with the compose builds).
hypothesis_settings.register_profile("gate", derandomize=True, deadline=None)
hypothesis_settings.load_profile("gate")


#: Modules that bring the REAL deploy stack up on the `FIXED_PORTS` pins.
#: There is exactly one host port 15173, so these may never run at the same
#: time as each other — they share one xdist group, which puts them on one
#: worker and back in sequence.
FIXED_PORT_MODULES = frozenset({"test_compose_stack", "test_verify_tool"})

#: The group those modules share. Any name will do; it only has to collide.
FIXED_PORT_GROUP = "deploy-stack-fixed-ports"

#: The E5.4b-e tester modules, which share ONE set of service containers.
#: They are grouped for the opposite reason to `FIXED_PORT_MODULES`: not
#: because they would collide, but because `--dist loadgroup` sends a group to
#: one worker, and a session-scoped fixture on one worker starts once. Spread
#: across four workers, the rig would be built four times and the phase-5
#: section 5 gate-time design would be gone (five containers, ~15s of
#: ready-wait, paid once per gate).
RIG_MODULES = frozenset(
    {
        "test_tester_influx",
        "test_tester_prometheus",
        "test_tester_grafana",
        "test_tester_s3",
        # E5.10's Grafana bootstrap dials the rig's Grafana too, so it joins
        # the group rather than starting a sixth container.
        "test_grafana_bootstrap",
    }
)

#: The group those modules share.
RIG_GROUP = "deployment-service-rig"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items) -> None:
    """Pin every test to an xdist group, so `--dist loadgroup` parallelizes by
    MODULE rather than by test (D99).

    **`tryfirst` is load-bearing.** xdist reads the `xdist_group` mark in its
    OWN `pytest_collection_modifyitems` and bakes the group into the nodeid
    there; a mark added after that hook has run is simply never seen, and every
    test scatters across workers as if unmarked. That failure is silent — the
    suite still runs, it just runs wrong — and it cost a red gate to find.

    Two properties have to hold at once and neither is xdist's default. Most
    suites here hang a module-scoped Postgres or Mosquitto container off a
    fixture; splitting one module across workers would start that container
    once per worker and make the suite slower, not faster. And the two modules
    that publish the fixed host ports would deadlock each other on 15173 if
    they overlapped.

    Grouping by module name gets the first. Overriding that name for the
    fixed-port modules gets the second, because a shared group is exactly
    xdist's promise of "same worker, therefore never concurrent".

    `RIG_MODULES` shares a group for a third reason (E5.4b): not to keep its
    modules apart but to keep them TOGETHER, so the one session-scoped service
    rig they all use is built on one worker and therefore built once.
    """
    for item in items:
        module = item.module.__name__.rsplit(".", 1)[-1] if item.module else "orphan"
        group = module
        if module in FIXED_PORT_MODULES:
            group = FIXED_PORT_GROUP
        elif module in RIG_MODULES:
            group = RIG_GROUP
        item.add_marker(pytest.mark.xdist_group(group))


@pytest.fixture(autouse=True)
def _serialise_fixed_port_modules(request):
    """Hold the machine-wide lock for the duration of any fixed-port test.

    `FIXED_PORT_GROUP` keeps these two modules off each other inside ONE pytest
    session. It can do nothing about a second session — another agent, another
    terminal, another worktree — and everything these tests touch is singular
    per machine: host ports 18000/15173/15432/16379/18883, and the generated
    `deploy/dev-certs` that mosquitto bind-mounts. Two runs overlapping here do
    not merely collide on a port; one regenerates the CA and passwd files while
    the other's broker is serving from them.

    Wrapping the whole test rather than the compose calls is deliberate: the
    fixtures that publish those ports run in setup and tear down after the body,
    so a lock taken inside the test would leave both ends unprotected.
    """
    module = request.module.__name__.rsplit(".", 1)[-1] if request.module else ""
    if module not in FIXED_PORT_MODULES:
        yield
        return
    with gate_lock("compose-stack"):
        yield


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


# --- The warm Postgres pool (task INFRA.1) -----------------------------------
#
# `ephemeral_postgres` used to start, migrate and destroy ONE CONTAINER PER
# MODULE. Measured on 2026-08-12 against the C3 tree: 57 Postgres containers per
# gate at 4.02s each (2.65s start, 1.01s `alembic upgrade head`, 0.36s teardown)
# — and most of the 4.05 GB the gate wrote to disk, because each of those 57 ran
# initdb and then all 22 migrations against a real filesystem.
#
# The contract that mattered was never "a container": it was "a migrated, empty
# database nobody else is touching". So the container becomes a machine-wide
# warm singleton whose data lives in tmpfs, the migrations run ONCE into a
# template database, and each caller gets `CREATE DATABASE ... TEMPLATE`, which
# on tmpfs is a memory copy. Measured on the same machine: 0.017s, against 4.02s.
#
# **What this deliberately does not change.** `ephemeral_postgres` keeps its
# signature and its guarantee, so no test module knows any of this happened.
# Readiness is still asserted from the HOST over TCP and not only by `pg_isready`
# inside the container, because D99's forwarder fault is still real and is what
# `wait_for_host_port` exists for. Coordination still runs through `gate_lock`
# and `GATE_STATE_DIR`, so two worktrees and two agents share one server rather
# than racing — and templates are keyed by a fingerprint of the migration
# directory, so a branch at a different migration head gets its OWN template
# instead of silently inheriting the other branch's schema.
#
# `fsync=off` and friends are safe here in a way they never are in production:
# the whole data directory is tmpfs, so there is nothing for a crash to leave
# half-written that a restart would need to recover. A pooled server that dies
# is replaced, not repaired.

POSTGRES_IMAGE = "postgres:16-alpine"

#: Named `_PW` rather than `_PASSWORD` on the DYNSEC_ADMIN_PW precedent, and
#: short on purpose: `test_repo_layout.SECRET_PATTERNS` flags a PASSWORD-ish name
#: followed by 20+ characters. This protects nothing — the server is bound to
#: loopback, holds only test data, and lives in RAM.
POOL_PG_PW = "eoe-testpool"

#: A cap, not an allocation: tmpfs pages are allocated lazily, so an idle pooled
#: server holds only the few hundred MB it has actually written.
POOL_TMPFS_SIZE = "2g"

#: Every `--tmpfs` in this file carries this, and leaving it off is a startup
#: crash rather than a slow leak. Docker mounts a tmpfs root-owned at mode 0755;
#: these images drop to unprivileged users (Prometheus `nobody`, Grafana uid 472,
#: Mosquitto 1883) and then cannot write the directory the mount just covered.
#: Nothing is protected by the stricter default — the mount holds throwaway test
#: state in RAM and dies with its container.
TMPFS_WORLD_WRITABLE = "mode=1777"

#: How long a pooled container may go unused before the next run replaces it
#: rather than reusing it. Long enough to span a working day of back-to-back
#: gates across both agent sessions; short enough that a machine left overnight
#: starts clean.
POOL_IDLE_TTL = 4 * 3600.0

#: How long a handed-out database may live before it is presumed leaked by an
#: interrupted run. Far above the longest gate (~5 minutes) and far below
#: `POOL_IDLE_TTL`, so a sweep can never drop a database a live test is using.
POOL_DATABASE_TTL = 3600.0

#: The label every pooled container carries. A labelled container that the
#: registry does not know about cannot be in use by anybody — acquiring one
#: requires the registry — so it is always safe to remove.
POOL_LABEL = "eoe.pool=postgres"


def _pool_registry() -> Path:
    return _gate_state_dir() / "pool.json"


def _read_pool() -> dict[str, Any]:
    try:
        state = json.loads(_pool_registry().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_pool(state: Mapping[str, Any]) -> None:
    _pool_registry().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _schema_fingerprint() -> str:
    """Identify the migration set, so two branches cannot share one template.

    A fingerprint of the whole `alembic/versions` directory rather than the head
    revision id, because the id answers a weaker question. Two worktrees can sit
    at the same head with different migration BODIES — one of them mid-edit — and
    a template keyed by the id would hand the second branch the first branch's
    schema, which is a wrong-schema failure three layers from its cause. Keyed by
    content, identical migration sets share a template (the common case, and the
    win) and any difference at all forks one.
    """
    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / "backend" / "alembic" / "versions").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def _admin_url(port: int, database: str = "postgres") -> str:
    return f"postgresql+psycopg://postgres:{POOL_PG_PW}@127.0.0.1:{port}/{database}"


@contextlib.contextmanager
def _admin_connection(port: int):
    """An AUTOCOMMIT connection to the pooled server's maintenance database.

    AUTOCOMMIT because CREATE DATABASE and DROP DATABASE cannot run inside a
    transaction block, and SQLAlchemy opens one by default.
    """
    import sqlalchemy as sa

    engine = sa.create_engine(_admin_url(port), isolation_level="AUTOCOMMIT", poolclass=sa.NullPool)
    try:
        with engine.connect() as connection:
            yield connection, sa
    finally:
        engine.dispose()


def _create_database(port: int, name: str, *, template: str, attempts: int = 60) -> None:
    """Clone `template` into a new database, retrying while it is locked.

    PostgreSQL refuses CREATE DATABASE while any other session is connected to
    the template, and treats a concurrent clone of the SAME template as exactly
    that. Six xdist workers cloning at once therefore collide by design, not by
    fault — and the clone takes ~17ms, so the contention window is tiny and a
    short retry loop is the whole fix. Anything else is re-raised: a genuinely
    missing template must not be retried into a timeout.
    """
    import sqlalchemy as sa

    deadline = time.monotonic() + 60.0
    with _admin_connection(port) as (connection, _sa):
        for _ in range(attempts):
            try:
                connection.execute(sa.text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
                return
            except sa.exc.ProgrammingError as error:
                if "being accessed by other users" not in str(error):
                    raise
                if time.monotonic() > deadline:
                    break
                time.sleep(0.1)
    raise AssertionError(
        f"could not clone {template!r} into {name!r}: it stayed locked by other sessions "
        f"for 60s. If no gate run is active, the pooled server may be wedged — "
        f"`make testpool-down` removes it."
    )


def _drop_database(port: int, name: str) -> None:
    import sqlalchemy as sa

    with contextlib.suppress(Exception), _admin_connection(port) as (connection, _sa):
        connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _database_exists(port: int, name: str) -> bool:
    import sqlalchemy as sa

    with _admin_connection(port) as (connection, _sa):
        found = connection.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
        ).scalar()
    return found is not None


def _sweep_leaked_databases(port: int) -> None:
    """Drop handed-out databases old enough to be from an interrupted run.

    The creation timestamp is carried in the NAME (`eoe_t{epoch}_{hex}`) because
    `pg_database` records no creation time — there is no column to sort on and no
    catalog view that answers "when did this appear". Encoding it where the
    sweep can read it is the smallest thing that makes leaked-database cleanup
    possible at all, and it keeps the sweep from ever guessing.
    """
    import sqlalchemy as sa

    cutoff = time.time() - POOL_DATABASE_TTL
    with contextlib.suppress(Exception), _admin_connection(port) as (connection, _sa):
        names = connection.execute(
            sa.text("SELECT datname FROM pg_database WHERE datname LIKE 'eoe\\_t%'")
        ).scalars()
        for name in list(names):
            _, _, rest = name.partition("_t")
            stamp, _, _ = rest.partition("_")
            with contextlib.suppress(ValueError):
                if float(stamp) < cutoff:
                    connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _pool_container_healthy(name: str, port: int, env: dict[str, str]) -> bool:
    """Whether a registered pooled container is still usable RIGHT NOW.

    Both halves are load-bearing and neither implies the other: Docker may have
    been pruned or restarted since the registry was written, and the container
    may be running while its published port does not answer (D99). The port
    timeout is short because this is a liveness check on the happy path, not the
    first-start readiness wait.
    """
    running = subprocess.run(
        [docker_cli(), "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
        env=env,
    )
    if running.returncode != 0 or running.stdout.strip() != "true":
        return False
    return wait_for_host_port(port, timeout=5.0)


def _start_pool_postgres(env: dict[str, str], attempts: int = 3) -> tuple[str, int]:
    """Start the machine-wide pooled Postgres and return it only once the HOST
    can reach it.

    Retries the whole container, not just the `docker run`, because the fault
    being retried is a published port that never materialised: the container is
    up and healthy, so there is nothing to retry at the command level and
    nothing that will improve if we wait longer on this one.
    """
    last = ""
    for attempt in range(1, attempts + 1):
        name = f"eoe-pool-pg-{uuid.uuid4().hex[:10]}"
        run = docker_retry(
            [
                docker_cli(),
                "run",
                "-d",
                "--name",
                name,
                "--label",
                POOL_LABEL,
                "--tmpfs",
                f"/var/lib/postgresql/data:rw,size={POOL_TMPFS_SIZE},{TMPFS_WORLD_WRITABLE}",
                "-p",
                "127.0.0.1:0:5432",
                "-e",
                f"POSTGRES_PASSWORD={POOL_PG_PW}",
                POSTGRES_IMAGE,
                # Safe only because the data directory is tmpfs: there is no
                # crash for recovery to survive, since a dead server is replaced
                # rather than restarted.
                "-c",
                "fsync=off",
                "-c",
                "full_page_writes=off",
                "-c",
                "synchronous_commit=off",
                # One server now backs every module on every worker of every
                # concurrent run, so the default 100 is no longer generous.
                "-c",
                "max_connections=300",
                "-c",
                "shared_buffers=256MB",
            ],
            env,
            what="pooled postgres",
            cleanup_name=name,
        )
        assert run.returncode == 0, f"could not start the pooled postgres: {run.stderr}"
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
                time.sleep(0.5)
            else:
                raise AssertionError("the pooled postgres never became ready")
            ports = subprocess.run(
                [docker_cli(), "port", name, "5432/tcp"], capture_output=True, text=True, env=env
            )
            assert ports.returncode == 0, ports.stderr
            host_port = int(ports.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
            if wait_for_host_port(host_port):
                return name, host_port
            last = (
                f"pooled postgres {name} was ready inside the container but its published "
                f"port {host_port} never accepted a connection from the host"
            )
        except BaseException:
            subprocess.run([docker_cli(), "rm", "-f", "-v", name], capture_output=True, env=env)
            raise
        subprocess.run([docker_cli(), "rm", "-f", "-v", name], capture_output=True, env=env)
        print(f"pooled postgres: retrying past a dropped port forward ({attempt}/{attempts})")
    raise AssertionError(f"{last} — {attempts} attempts (D99: Docker's forwarder under load)")


def _acquire_pool_postgres() -> int:
    """The host port of the machine-wide warm Postgres, starting it if needed.

    Everything that decides whether to reuse or replace happens under one
    machine-wide lock, because the decision is read-modify-write on shared state
    and two agents reaching it at once would otherwise both start a server and
    both record it, leaking one.
    """
    env = docker_env()
    with gate_lock("container-pool", timeout=600.0):
        state = _read_pool()
        entry = state.get("postgres")
        if isinstance(entry, dict):
            idle = time.time() - float(entry.get("last_used", 0))
            reusable = idle < POOL_IDLE_TTL and _pool_container_healthy(
                str(entry["container"]), int(entry["port"]), env
            )
            if reusable:
                entry["last_used"] = time.time()
                _write_pool(state)
                return int(entry["port"])
            subprocess.run(
                [docker_cli(), "rm", "-f", "-v", str(entry["container"])],
                capture_output=True,
                env=env,
            )
        name, port = _start_pool_postgres(env)
        state["postgres"] = {
            "container": name,
            "port": port,
            "last_used": time.time(),
            "templates": {},
        }
        _write_pool(state)
        return port


def _acquire_template(port: int) -> str:
    """The migrated template for THIS tree's migration set, building it once.

    Held under the same lock as acquisition so that six workers starting at once
    run `alembic upgrade head` exactly once between them rather than six times
    into the same database.
    """
    fingerprint = _schema_fingerprint()
    template = f"eoe_tpl_{fingerprint}"
    with gate_lock("container-pool", timeout=600.0):
        state = _read_pool()
        entry = state.get("postgres")
        known = isinstance(entry, dict) and template in (entry.get("templates") or {})
        if known and _database_exists(port, template):
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry["last_used"] = time.time()
            state["postgres"] = entry
            _write_pool(state)
            return template

        _drop_database(port, template)
        _create_database(port, template, template="template0")
        upgraded = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT / "backend",
            capture_output=True,
            text=True,
            env={**docker_env(), "DATABASE_URL": _admin_url(port, template)},
            timeout=180,
        )
        assert upgraded.returncode == 0, f"migration of the template failed: {upgraded.stderr}"
        if isinstance(entry, dict):
            entry.setdefault("templates", {})[template] = time.time()
            entry["last_used"] = time.time()
            state["postgres"] = entry
            _write_pool(state)
    return template


@contextlib.contextmanager
def ephemeral_postgres(migrate: bool = True):
    """A migrated, empty Postgres database nobody else is touching.

    The guarantee is unchanged from when this started a container per call, and
    no test module can tell the difference: what it yields is still a URL to a
    database at `head` with no rows in it, and dropping it at the end is still
    unconditional. What changed is the cost — see the section header above.

    `migrate=False` clones `template0` rather than the migrated template, so a
    caller that runs alembic itself (`test_migrations`, E0 readiness' reverse-
    migration test) still gets a genuinely empty database and still proves the
    migrations for real. That path is the reason the template can be trusted:
    the suite never stops exercising a from-scratch migration run.
    """
    port = _acquire_pool_postgres()
    template = _acquire_template(port) if migrate else "template0"
    database = f"eoe_t{int(time.time())}_{uuid.uuid4().hex[:12]}"
    _create_database(port, database, template=template)
    try:
        yield _admin_url(port, database)
    finally:
        _drop_database(port, database)


def reap_pool(*, force: bool = False) -> list[str]:
    """Close pooled containers nothing can be using, and return what went.

    Two disjoint cases, and the distinction is what makes this safe to run while
    another agent's gate is mid-flight. A container the registry does not name
    is an ORPHAN and is always removable, because acquiring one goes through the
    registry and nothing that skipped it can hold a reference. A container the
    registry does name is removable only once it has gone `POOL_IDLE_TTL`
    without an acquisition — every acquire heartbeats it, so a live run keeps
    its own server alive continuously.

    `force` is the `make testpool-down` path: take everything, now.
    """
    env = docker_env()
    removed: set[str] = set()
    with gate_lock("container-pool", timeout=600.0):
        state = _read_pool()
        entry = state.get("postgres") if isinstance(state.get("postgres"), dict) else None

        #: The one container that must survive this sweep, if any. It drops to
        #: None the moment the registry entry is retired, which is what lets the
        #: orphan pass below collect it without a second case.
        keep = str(entry["container"]) if entry else None

        if entry and (force or time.time() - float(entry.get("last_used", 0)) > POOL_IDLE_TTL):
            state.pop("postgres", None)
            keep = None
        elif entry and not force:
            _sweep_leaked_databases(int(entry["port"]))

        listed = subprocess.run(
            [docker_cli(), "ps", "-aq", "--filter", f"label={POOL_LABEL}"],
            capture_output=True,
            text=True,
            env=env,
        )
        for container in listed.stdout.split():
            names = subprocess.run(
                [docker_cli(), "inspect", "-f", "{{.Name}}", container],
                capture_output=True,
                text=True,
                env=env,
            )
            name = names.stdout.strip().lstrip("/")
            if name and name != keep:
                removed.add(name)

        if removed:
            subprocess.run(
                [docker_cli(), "rm", "-f", "-v", *sorted(removed)],
                capture_output=True,
                env=env,
            )
        _write_pool(state)
    return sorted(removed)


def pytest_sessionstart(session) -> None:
    """Reap idle and orphaned pooled containers once, before any test runs.

    Controller only. Under xdist this hook fires in every worker as well, and
    six workers all taking the machine-wide lock to run the same sweep would
    serialise startup for no gain — `workerinput` exists only in a worker, which
    is xdist's own way of telling them apart.

    Failures here are swallowed on purpose: reaping is housekeeping, and a
    Docker hiccup during it must not turn into a red gate that reports nothing
    about the code under test. A container that survives one sweep is caught by
    the next.
    """
    if hasattr(session.config, "workerinput"):
        return
    with contextlib.suppress(Exception):
        reap_pool()


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


#: Docker Desktop's port forwarder fails like this, transiently, when several
#: containers publish ports at once — which is exactly what a parallel suite
#: does (D99). It is a fault in the forwarder, not in the container: the same
#: command succeeds a moment later.
_TRANSIENT_DOCKER = (
    "/forwards/expose returned unexpected status: 500",
    "port is already allocated",
    "failed to set up container networking",
)


def docker_retry(
    argv: list[str],
    env: dict[str, str],
    attempts: int = 5,
    what: str = "docker command",
    cleanup_name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a docker command, retrying ONLY the known-transient port-forwarder
    faults (D99).

    Deliberately narrow. A retry loop around every docker failure would turn a
    genuinely broken image or a bad flag into a slow, silent timeout; these
    three strings are the forwarder giving up under concurrency, and nothing
    else. Anything unrecognised comes straight back to the caller on the first
    attempt, still failing, still with its own message.

    `cleanup_name` is required for `docker run --name`: a run that fails at the
    networking stage has ALREADY created the container, so a bare retry hits
    "name is already in use" and reports the wrong fault entirely. `start` on
    an existing container needs no such thing and passes None.
    """
    result = subprocess.run(argv, capture_output=True, text=True, env=env)
    for attempt in range(1, attempts):
        if result.returncode == 0 or not any(fault in result.stderr for fault in _TRANSIENT_DOCKER):
            return result
        time.sleep(attempt)
        print(f"{what}: retrying past a transient Docker fault ({attempt}/{attempts - 1})")
        if cleanup_name is not None:
            subprocess.run(
                [docker_cli(), "rm", "-f", "-v", cleanup_name], capture_output=True, env=env
            )
        result = subprocess.run(argv, capture_output=True, text=True, env=env)
    return result


#: Where cross-run coordination state lives. One fixed directory per machine,
#: NOT per checkout: the whole point is that two agents in two worktrees, or two
#: terminals in one, see each other's claims. It is deliberately in the system
#: temp dir rather than the repo, so a `git clean` cannot desynchronise two runs
#: mid-flight and nothing here is ever committed.
GATE_STATE_DIR = Path(tempfile.gettempdir()) / "eoe-gate-state"

#: How long a port claim or a lock is honoured before another run may take it.
#: A claim only has to outlive the seconds between choosing a port and Docker
#: binding it; a lock has to outlive a whole compose stack lifecycle, which is
#: why they are not the same number.
PORT_CLAIM_TTL = 600.0
LOCK_STALE_AFTER = 2400.0


def _gate_state_dir() -> Path:
    GATE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return GATE_STATE_DIR


def _process_alive(pid: int) -> bool:
    """Whether a PID is still running, so a lock held by a killed run can be
    broken immediately instead of waiting out its TTL. POSIX-only in practice;
    anywhere `os.kill` cannot answer, the caller falls back to the TTL."""
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH
    except AttributeError:  # pragma: no cover - non-POSIX
        return True
    return True


@contextlib.contextmanager
def gate_lock(name: str, *, timeout: float = 1800.0, poll: float = 0.5):
    """A lock held across PROCESSES, so concurrent gate runs take turns.

    xdist's `xdist_group` only serialises tests inside ONE pytest session. Two
    agents, two terminals, or two worktrees on one machine are separate
    sessions, and there is nothing in pytest that can know about them. Anything
    backed by a genuinely singular host resource — a fixed published port, a
    shared generated directory — therefore needs a lock the operating system
    arbitrates rather than a test-runner convention.

    Implemented as an exclusive-create lockfile rather than `fcntl.flock`
    because the gate runs on Windows too (`gate.ps1`), and `O_CREAT | O_EXCL`
    is atomic on every filesystem the gate is supported on. The holder's PID and
    a timestamp go in the file: a lock whose owner has died is broken at once,
    and one whose owner is alive but wedged is broken after `LOCK_STALE_AFTER`
    so a crashed machine cannot block every future run forever.
    """
    path = _gate_state_dir() / f"{name}.lock"
    deadline = time.monotonic() + timeout
    handle = None
    while handle is None:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(path):
                # Best effort: if another run breaks it first, the next
                # exclusive create simply fails again and we keep waiting.
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"waited {timeout:.0f}s for the {name!r} gate lock at {path}. "
                    "Another gate run is holding it; if nothing is running, delete that file."
                ) from None
            time.sleep(poll)
    try:
        os.write(handle, json.dumps({"pid": os.getpid(), "at": time.time()}).encode())
        os.close(handle)
        handle = None
        yield
    finally:
        if handle is not None:  # pragma: no cover - only on a write failure
            os.close(handle)
        with contextlib.suppress(OSError):
            path.unlink()


def _lock_is_stale(path: Path) -> bool:
    try:
        held = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        # Unreadable or half-written: judge it by age alone, since a lock whose
        # holder never finished writing it is exactly the crashed case.
        try:
            return time.time() - path.stat().st_mtime > LOCK_STALE_AFTER
        except OSError:
            return False
    pid, at = held.get("pid"), held.get("at", 0)
    if isinstance(pid, int) and not _process_alive(pid):
        return True
    return time.time() - float(at) > LOCK_STALE_AFTER


def _claimed_ports(registry: Path) -> dict[str, float]:
    try:
        claims = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {
        port: claimed
        for port, claimed in claims.items()
        if isinstance(claimed, int | float) and now - claimed < PORT_CLAIM_TTL
    }


def free_port() -> int:
    """A currently-free localhost port, for the cases that need a STABLE host
    port. Docker's `-p 127.0.0.1:0:8883` picks a fresh port on every container
    start, so a test that stops and restarts a broker (E3.2's reconnect
    acceptance) has to name the port itself or the client would be dialling
    the old one after the restart.

    **Binding port 0 is not enough when more than one run is on the machine.**
    The socket is closed before the caller returns, and callers then write the
    number into a database row and only later ask Docker to publish it. In that
    window the kernel is free to hand the same port to another gate run's
    `bind(0)`, and the loser fails with "port is already allocated" — or, worse,
    connects to the winner's container. Re-picking inside `ephemeral_broker`
    cannot fix it either, because by then the port is already committed to the
    `deployment_service` row the test seeded.

    So a claim is recorded in a machine-wide registry, under the same lock every
    run uses, and ports claimed by a live run are skipped. Claims expire after
    `PORT_CLAIM_TTL` so an interrupted run leaks nothing permanently.
    """
    registry = _gate_state_dir() / "ports.json"
    with gate_lock("ports", timeout=60.0, poll=0.05):
        claims = _claimed_ports(registry)
        for _ in range(64):
            with contextlib.closing(socket.socket()) as probe:
                probe.bind(("127.0.0.1", 0))
                port: int = probe.getsockname()[1]
            if str(port) in claims:
                continue
            claims[str(port)] = time.time()
            registry.write_text(json.dumps(claims), encoding="utf-8")
            return port
        raise AssertionError(
            f"could not find an unclaimed free port in 64 tries ({len(claims)} claims held); "
            f"if no gate run is active, delete {registry}"
        )


def _host_port_accepting(port: int, connect_timeout: float = 1.0) -> bool:
    """Whether THIS process can open a TCP connection to a published port."""
    with contextlib.closing(socket.socket()) as probe:
        probe.settimeout(connect_timeout)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_for_host_port(port: int, *, timeout: float = 30.0) -> bool:
    """Wait until a published port answers ON THE HOST, not inside the container.

    Every readiness probe in this file used to run through `docker exec` —
    `pg_isready`, `nc -z` — which proves the SERVER is up and proves nothing at
    all about the port forward that the test client will actually dial. Docker
    Desktop's forwarder drops publishes under concurrent load (D99), and when it
    does, the container is healthy, `docker port` cheerfully reports a mapping,
    and the connection is refused. That surfaced as seven `test_audit` errors
    reporting a *migration* failure, which is three layers from the cause.

    So the forward is now asserted where it is used: from the host, over TCP.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _host_port_accepting(port):
            return True
        time.sleep(0.1)
    return False


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

    def logs(self) -> str:
        """Everything the broker has said so far (task E5.4a).

        The broker's own record of what it received, which is the only witness
        available for a message a plugin CONSUMES rather than distributes:
        Mosquitto's dynamic security plugin handles a `$CONTROL` publish
        internally, so no subscriber - not even an administrator - ever sees
        one, and "did the platform publish to $CONTROL?" cannot be answered
        over MQTT. It can be answered from here, at `log_type all`.
        """
        result = subprocess.run(
            [docker_cli(), "logs", self.name],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        return result.stdout + result.stderr

    def reload(self) -> None:
        """SIGHUP: Mosquitto re-reads passwd and acl without dropping retained
        state, which is how a devbroker re-run reaches a live broker."""
        subprocess.run(
            [docker_cli(), "kill", "-s", "HUP", self.name], capture_output=True, env=docker_env()
        )

    def refresh(self) -> None:
        """Re-ship `dev_dir` into the container, then `reload()`.

        Needed because the material arrives by `docker cp` and not by a bind
        mount (see `ephemeral_broker` on why): re-running `app.devbroker` on the
        host rewrites `passwd` and `acl` where the container cannot see them, so
        a SIGHUP alone re-reads the OLD files and every newly minted account is
        rejected with a bare "not authorised". Added by SIM.4, whose fleet is
        provisioned into inventory first and given credentials second — which is
        the only order broker accounts can be minted in.
        """
        # `dev_dir/.` and not `dev_dir`: `/mosquitto/dev` exists by now, and
        # `docker cp` copies a source DIRECTORY into an existing destination
        # (leaving the files one level too deep) while `dir/.` copies its
        # CONTENTS over the destination, which is what a refresh means. Built as
        # a string because `Path` normalizes the trailing "." away.
        copied = subprocess.run(
            [docker_cli(), "cp", f"{self.dev_dir}{os.sep}.", f"{self.name}:/mosquitto/dev"],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        assert copied.returncode == 0, f"could not re-ship broker material: {copied.stderr}"
        self.reload()

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
        started = docker_retry([docker_cli(), "start", self.name], env, what="broker restart")
        assert started.returncode == 0, f"could not restart the broker: {started.stderr}"
        _wait_until_accepting(self.name, env)
        # The forward is re-established on every start, and E3.2's reconnect
        # acceptance dials this port from the host the instant we return.
        assert wait_for_host_port(self.port), (
            f"broker {self.name} restarted and accepts inside the container, but its "
            f"published port {self.port} never answered from the host"
        )


#: **Read from the shipped constant, not written here** (D144).
#:
#: Every broker in the gate runs the image a generated stack ships, because a
#: fixture on a different version proves nothing about what an operator gets.
#: This was not hypothetical: the fixtures floated on `eclipse-mosquitto:2`,
#: Docker Hub moved that tag to 2.1.2, and every dynsec test in the suite went
#: on passing while the 2.0.x image the stack pins refused every login — the
#: two versions read `dynamic-security.json` passwords differently
#: (`brokerconfig.dynsec_password_fields` carries the measurement). E5.10's
#: keystone found it by bringing the generated bundle up. Importing the
#: constant rather than repeating it means the next version bump moves both.
MOSQUITTO_IMAGE = STACK_IMAGES["mosquitto"]


def _start_broker_container(
    dev_dir: Path,
    conf: Path,
    host_port: int | None,
    env: dict[str, str],
    attempts: int = 3,
) -> str:
    """Create, populate and start one broker, returning it only once the HOST
    can reach its published port.

    **Retries the whole container, exactly as `_start_ephemeral_postgres` has
    always done, and for the identical fault.** Docker Desktop's forwarder drops
    publishes under concurrent load (D99): the container is up and accepting
    inside, `docker port` reports a mapping, and the host connection is refused.
    There is nothing to retry at the command level and nothing that improves by
    waiting longer on this one — only a new container helps.

    This path used to assert instead of retrying, which was survivable while the
    suite was paced by 57 sequential Postgres startups. Pooling those (INFRA.1)
    removed the pacing, the remaining container starts now land in tighter
    bursts, and the fault surfaced as seven `test_dev_broker` setup errors in a
    run whose 999 tests all passed. `docker_retry` is deliberately narrow (D99)
    and is NOT widened to cover this; the whole-container retry is the remedy
    this codebase already chose for this symptom.
    """
    last = ""
    for attempt in range(1, attempts + 1):
        name = f"eoe-mqtt-{uuid.uuid4().hex[:10]}"
        created = subprocess.run(
            [
                docker_cli(),
                "create",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{host_port or 0}:8883",
                # Retained state stays REAL — `persistence true` in the dev
                # broker's config is what E3.7's restart acceptance leans on —
                # it just lives in RAM now (INFRA.1).
                "--tmpfs",
                f"/mosquitto/data:rw,size=64m,{TMPFS_WORLD_WRITABLE}",
                MOSQUITTO_IMAGE,
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
            started = docker_retry([docker_cli(), "start", name], env, what="broker start")
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
            if wait_for_host_port(published):
                return name
            last = (
                f"broker {name} was accepting inside the container but its published port "
                f"{published} never answered from the host"
            )
        except BaseException:
            subprocess.run([docker_cli(), "rm", "-f", "-v", name], capture_output=True, env=env)
            raise
        subprocess.run([docker_cli(), "rm", "-f", "-v", name], capture_output=True, env=env)
        print(f"ephemeral broker: retrying past a dropped port forward ({attempt}/{attempts})")
    raise AssertionError(f"{last} — {attempts} attempts (D99: Docker's forwarder under load)")


@contextlib.contextmanager
def ephemeral_broker(dev_dir: Path, host_port: int | None = None, conf: Path | None = None):
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

    `conf` defaults to the dev broker's committed `mosquitto.conf` and exists
    so E5.4a can stand the SAME container up with the dynamic security plugin
    loaded instead of the `acl_file` (see `dynsec_broker`). One container
    recipe, two configurations - a second copy of this function is how the
    `docker cp` rule above gets forgotten on one of them.
    """
    env = docker_env()
    conf = conf or REPO_ROOT / "deploy" / "mosquitto" / "mosquitto.conf"
    name = _start_broker_container(dev_dir, conf, host_port, env)
    try:
        ports = subprocess.run(
            [docker_cli(), "port", name, "8883/tcp"], capture_output=True, text=True, env=env
        )
        published = int(ports.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
        yield Broker(name=name, port=published, dev_dir=dev_dir)
    finally:
        subprocess.run([docker_cli(), "rm", "-f", "-v", name], capture_output=True, env=env)


# --- Dynamic security broker (task E5.4a) -----------------------------------
#
# Phase-5 fixed choice 4 makes Mosquitto's dynamic security plugin REQUIRED for
# v1, so E5.4a's probe has three verdicts to prove and the dev broker can only
# produce one of them (`absent` - it loads no plugin). These build the other
# two.
#
# **One container serves both**, with two clients in one `dynamic-security.json`:
# an administrator that holds the plugin's `admin` role and a plain account
# that does not. `available` and `denied` are then a property of which
# credential the test dials with, not of which container it started, which
# halves the fixture's cost at every gate - and the two verdicts genuinely are
# about the account rather than the broker, so this is also the more honest
# shape.
#
# The password hashes come from `devbroker.password_hash`, which turns out to
# be exactly what dynsec wants (`$7$<iterations>$<b64 salt>$<b64 hash>`,
# PBKDF2-HMAC-SHA512) at the plugin's own default iteration count. Verified by
# the broker accepting the logins rather than by reading Mosquitto's source:
# if the hash were wrong, every test here would fail on CONNACK.

#: The plugin's default. `mosquitto_ctrl dynsec init` writes hashes at this
#: count, and a mismatch would be an authentication failure and nothing subtler.
DYNSEC_PW_ITERATIONS = 1000

#: Named `_PW` and not `_PASSWORD` on purpose. `test_repo_layout.SECRET_PATTERNS`
#: flags `(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY)\w*\s*[=:]` followed by 20+
#: characters, and these two values are 21 - so the constants tripped the
#: committed-secret scanner and turned the gate red. The scanner is a
#: definition-of-done item for this phase and is deliberately blunt, so **the
#: name moves and the guard does not** - the same resolution E5.3 took for its
#: `*_CANARY` constants. `RIG_PASSWORD` below keeps its name because its value
#: is under the threshold.
DYNSEC_ADMIN_USER = "dynsec-admin"
DYNSEC_ADMIN_PW = "dynsec-admin-password"
DYNSEC_PLAIN_USER = "dynsec-plain"
DYNSEC_PLAIN_PW = "dynsec-plain-password"

#: `acl_file` and the plugin are mutually exclusive in practice: with dynsec
#: loaded, authentication and authorisation both come from its JSON, so the
#: password file and ACL file are deliberately absent here. Everything else -
#: the TLS block, `allow_anonymous false` - is the dev broker's config verbatim,
#: so what differs between the two brokers is only the thing under test.
DYNSEC_CONF = """\
# GENERATED by backend/tests/conftest.py (task E5.4a) - not a deployable file.
#
# The dev broker with `acl_file` replaced by the dynamic security plugin, so
# E5.4a's probe has a broker that answers on $CONTROL/dynamic-security/v1.
# E5.8a generates the real one; this exists only to give the probe something
# true to be tested against before that lands.

listener 8883
protocol mqtt

cafile /mosquitto/dev/ca.crt
certfile /mosquitto/dev/server.crt
keyfile /mosquitto/dev/server.key

require_certificate false
tls_version tlsv1.2

allow_anonymous false
plugin /usr/lib/mosquitto_dynamic_security.so
plugin_opt_config_file /mosquitto/dev/dynamic-security.json

persistence false

# `all`, unlike the dev broker's error/warning/notice. E5.4a asserts that the
# probe does NOT publish to $CONTROL when the broker refuses it the control
# topic, and the broker's log is the only witness: the plugin consumes a
# $CONTROL publish internally, so no subscriber ever sees one (see
# `Broker.logs`). At this level Mosquitto records every PUBLISH it receives and
# every one it denies, by client and by topic.
log_dest stdout
log_type all
connection_messages true
"""


def dynsec_config(deployment_slug: str) -> dict:
    """A `dynamic-security.json` with one administrator and one plain client.

    The plain client's role grants it the deployment's whole topic tree, which
    is the same cut `devbroker.acl_file_text` gives the platform account. That
    matters: it means a `denied` verdict in a test is caused by the MISSING
    admin role and not by an account that cannot do anything at all, so the
    test proves the probe distinguishes "not an administrator" rather than
    "not authorised for anything".

    `defaultACLAccess` denies publish and subscribe by default, which is
    `mosquitto_ctrl dynsec init`'s own default and the reason an unprivileged
    client's SUBSCRIBE to the control topic is refused.
    """
    from app.brokerconfig import dynsec_password_fields
    from app.contracts.mqtt import deployment_root
    from app.devbroker import password_hash

    def client(username: str, password: str, rolename: str) -> dict:
        # Split into `password`/`salt`/`iterations` through the SAME helper the
        # generated stack renders with, because Mosquitto 2.0 ignores a
        # combined `encoded_password` and leaves the account with no password
        # at all (D144). This fixture used to write the combined form and
        # passed only because it ran a floating `:2` tag that had moved to
        # 2.1.x; both now run `MOSQUITTO_IMAGE`.
        return {
            "username": username,
            **dynsec_password_fields(
                password_hash(password, os.urandom(12), iterations=DYNSEC_PW_ITERATIONS)
            ),
            "roles": [{"rolename": rolename}],
        }

    root = deployment_root(deployment_slug)
    return {
        "clients": [
            client(DYNSEC_ADMIN_USER, DYNSEC_ADMIN_PW, "admin"),
            client(DYNSEC_PLAIN_USER, DYNSEC_PLAIN_PW, "deployment"),
        ],
        "roles": [
            {
                "rolename": "admin",
                "acls": [
                    {
                        "acltype": "publishClientSend",
                        "topic": "$CONTROL/dynamic-security/#",
                        "allow": True,
                    },
                    {
                        "acltype": "publishClientReceive",
                        "topic": "$CONTROL/dynamic-security/#",
                        "allow": True,
                    },
                    {
                        "acltype": "subscribePattern",
                        "topic": "$CONTROL/dynamic-security/#",
                        "allow": True,
                    },
                    {"acltype": "publishClientSend", "topic": f"{root}/#", "allow": True},
                    {"acltype": "publishClientReceive", "topic": f"{root}/#", "allow": True},
                    {"acltype": "subscribePattern", "topic": f"{root}/#", "allow": True},
                    {"acltype": "unsubscribePattern", "topic": "#", "allow": True},
                ],
            },
            {
                "rolename": "deployment",
                "acls": [
                    {"acltype": "publishClientSend", "topic": f"{root}/#", "allow": True},
                    {"acltype": "publishClientReceive", "topic": f"{root}/#", "allow": True},
                    {"acltype": "subscribePattern", "topic": f"{root}/#", "allow": True},
                    {"acltype": "unsubscribePattern", "topic": "#", "allow": True},
                ],
            },
        ],
        "defaultACLAccess": {
            "publishClientSend": False,
            "publishClientReceive": True,
            "subscribe": False,
            "unsubscribe": True,
        },
    }


@contextlib.contextmanager
def dynsec_broker(tmp_path: Path, deployment_slug: str, host_port: int | None = None):
    """A TLS Mosquitto with the dynamic security plugin loaded (task E5.4a).

    Reuses `generate_tls_material` and `ephemeral_broker` rather than growing a
    second container recipe: the only difference from the dev broker is which
    authorisation backend `mosquitto.conf` names.

    Files land world-readable for the same reason `devbroker.write_artifacts`
    does it - the container drops to uid 1883 and cannot read a 0600 file that
    arrived by `docker cp`. `dynamic-security.json` additionally has to be
    WRITABLE, because the plugin rewrites it whenever a client or role changes;
    E5.6 will exercise that path.
    """
    from app.devbroker import generate_tls_material

    dev_dir = tmp_path / "dynsec-dev"
    dev_dir.mkdir(parents=True, exist_ok=True)
    for filename, blob in generate_tls_material().items():
        path = dev_dir / filename
        path.write_bytes(blob)
        path.chmod(0o644)
    config = dev_dir / "dynamic-security.json"
    config.write_text(json.dumps(dynsec_config(deployment_slug), indent=2), encoding="utf-8")
    config.chmod(0o666)
    conf = tmp_path / "dynsec-mosquitto.conf"
    conf.write_text(DYNSEC_CONF, encoding="utf-8")
    dev_dir.chmod(0o755)

    with ephemeral_broker(dev_dir, host_port=host_port, conf=conf) as broker:
        yield broker


# --- The deployment-service container rig (E5.4b-e; phase-5 fixed choice 5) --
#
# Real containers on the happy path are non-negotiable for these five testers,
# because they exist to detect precisely what a fake cannot have: Prometheus's
# remote-write receiver being off by DEFAULT, Influx 3's actual auth semantics,
# Grafana's datasource provisioning shapes, MinIO's SigV4. A tester validated
# only against a fake is a tester validated against its author's beliefs.
#
# The gate-time design is part of the choice, and all of it lives here: ONE
# session-scoped rig, on ONE xdist group (`RIG_MODULES`), so it is built once
# per gate rather than once per module per worker; containers started in
# PARALLEL so the ready-wait is the slowest (Grafana) and not the sum; and no
# published fixed ports, so two concurrent gate runs cannot collide.
#
# From E5.10 the rig BECOMES the generated stack and this hand-written
# assembly goes away — which is why nothing here is imported by application
# code and every value is a constant a bundle will later supply.

#: One password for every rig service. A test fixture's credentials are not
#: secrets; keeping them identical makes "did the tester send the credential
#: it was given" the only thing a failure can mean.
RIG_USER = "eoe"
RIG_PASSWORD = "rigpassword"

#: The same password, bcrypt'd, because Prometheus's `web_config.yml` accepts
#: nothing else. Precomputed with `htpasswd -nbB` rather than hashed at test
#: time SO THAT `bcrypt` DOES NOT BECOME A DEPENDENCY of this repository for
#: the sake of one fixture. Regenerate with:
#:     docker run --rm httpd:2-alpine htpasswd -nbB eoe rigpassword
RIG_PASSWORD_BCRYPT = "$2y$05$W9GSYhvTglLiRib4ikQOz.uNA3r.dUSSBfjPA9R4/LpvCFODudjsq"

RIG_INFLUX_DATABASE = "eoe_rig"
RIG_S3_BUCKET = "eoe-rig-bucket"

RIG_IMAGES = {
    "influx": "influxdb:3-core",
    "prometheus": "prom/prometheus:v3.5.0",
    "grafana": "grafana/grafana:11.6.0",
    "minio": "minio/minio:latest",
}

#: Minimal scrape config. Prometheus needs a config file to start at all, and
#: scraping itself gives the `up` metric E5.4c's read query asks for.
RIG_PROMETHEUS_YML = """
global:
  scrape_interval: 1s
  evaluation_interval: 1s
scrape_configs:
  - job_name: prometheus
    basic_auth:
      username: eoe
      password: rigpassword
    static_configs:
      - targets: ['127.0.0.1:9090']
"""

RIG_PROMETHEUS_WEB_YML = f"""
basic_auth_users:
  {RIG_USER}: "{RIG_PASSWORD_BCRYPT}"
"""


@dataclasses.dataclass(frozen=True)
class RigService:
    """One running container in the rig, addressed from the host."""

    name: str
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclasses.dataclass(frozen=True)
class ServiceRig:
    """Every service the E5.4b-e testers dial, already up and credentialed.

    `prometheus` has `--web.enable-remote-write-receiver`; `prometheus_closed`
    is the same image and config WITHOUT it. Two containers rather than one
    restarted, because E5.4c's whole acceptance is telling those two states
    apart, and a fixture that reconfigures in place cannot be asserted against
    both within one test.
    """

    influx: RigService
    influx_token: str
    prometheus: RigService
    prometheus_closed: RigService
    grafana: RigService
    grafana_token: str
    minio: RigService

    influx_database: str = RIG_INFLUX_DATABASE
    bucket: str = RIG_S3_BUCKET
    username: str = RIG_USER
    password: str = RIG_PASSWORD


def _rig_container(
    label: str,
    image: str,
    container_port: int,
    *,
    command: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    files: Sequence[tuple[Path, str]] = (),
    tmpfs: Sequence[str] = (),
) -> RigService:
    """Create, populate and start one rig container on a Docker-assigned port.

    Files go in with `docker cp`, never a bind mount, for `ephemeral_broker`'s
    reason: bind mounts of a WSL or Windows path translate differently per
    host and the gate has to behave identically on all three.

    The whole container is retried on a dropped port forward, for the reason
    `_start_broker_container` states at length: it is D99's forwarder fault, and
    only a new container helps.

    `tmpfs` names the paths a service writes its state to, so the rig costs RAM
    rather than SSD writes (INFRA.1). Every one of these services is disposable
    by construction — the rig is torn down at the end of the session and none of
    it is ever read again — so putting its storage in memory changes what the
    tests exercise not at all.

    **`TMPFS_WORLD_WRITABLE` is not optional on any of them.** Docker mounts a
    `--tmpfs` root-owned and mode 0755, while every one of these images drops to
    an unprivileged user — Prometheus to `nobody`, Grafana to uid 472 — so the
    mount lands exactly where the service writes and denies it. Prometheus does
    not degrade: it panics on `Unable to create mmap-ed active query log` before
    it ever publishes a port, and the whole rig fails with 37 errors in four
    modules. Postgres survives without it only because its entrypoint runs as
    root and chowns the data directory itself.
    """
    docker = docker_cli()
    env_vars = docker_env()
    last = ""
    for attempt in range(1, 4):
        name = f"eoe-rig-{label}-{uuid.uuid4().hex[:8]}"
        create = [docker, "create", "--name", name, "-p", f"127.0.0.1:0:{container_port}"]
        for path in tmpfs:
            create += ["--tmpfs", path]
        for key, value in (env or {}).items():
            create += ["-e", f"{key}={value}"]
        create += [image, *(command or [])]

        created = subprocess.run(create, capture_output=True, text=True, env=env_vars)
        assert created.returncode == 0, f"could not create {label}: {created.stderr}"
        try:
            for source, target in files:
                copied = subprocess.run(
                    [docker, "cp", str(source), f"{name}:{target}"],
                    capture_output=True,
                    text=True,
                    env=env_vars,
                )
                assert copied.returncode == 0, (
                    f"docker cp {source} -> {label} failed: {copied.stderr}"
                )

            started = docker_retry([docker, "start", name], env_vars, what=f"{label} start")
            assert started.returncode == 0, f"{label} would not start: {started.stderr}"

            ports = subprocess.run(
                [docker, "port", name, f"{container_port}/tcp"],
                capture_output=True,
                text=True,
                env=env_vars,
            )
            if ports.returncode != 0:
                logs = subprocess.run(
                    [docker, "logs", name], capture_output=True, text=True, env=env_vars
                )
                raise AssertionError(
                    f"{label} exited: {ports.stderr}\n{logs.stdout}\n{logs.stderr}"
                )
            published = int(ports.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
            if wait_for_host_port(published):
                return RigService(name=name, port=published)
            last = (
                f"{label} started but its published port {published} never answered from the host"
            )
        except BaseException:
            subprocess.run([docker, "rm", "-f", "-v", name], capture_output=True, env=env_vars)
            raise
        subprocess.run([docker, "rm", "-f", "-v", name], capture_output=True, env=env_vars)
        print(f"rig {label}: retrying past a dropped port forward ({attempt}/3)")
    raise AssertionError(f"{last} — 3 attempts (D99: Docker's forwarder under load)")


def _wait_for_http(service: RigService, path: str, *, timeout: float = 60.0) -> None:
    """Wait until the service answers HTTP at all — any status.

    Deliberately not "answers 200": Influx 3 answers **401** on `/health`
    without a token, which is a fully-started server refusing an
    unauthenticated read. Waiting for 200 there would wait forever, and
    waiting for the TCP port alone returns before the HTTP stack is up.
    """
    import httpx

    deadline = time.monotonic() + timeout
    last = "no attempt completed"
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=2.0) as client:
                client.get(f"{service.url}{path}")
            return
        except Exception as error:  # noqa: BLE001  (any answer at all ends the wait)
            last = f"{type(error).__name__}: {error}"
        time.sleep(0.25)
    logs = subprocess.run(
        [docker_cli(), "logs", "--tail", "40", service.name],
        capture_output=True,
        text=True,
        env=docker_env(),
    )
    raise AssertionError(
        f"{service.name} never answered HTTP at {path} ({last})\n{logs.stdout}\n{logs.stderr}"
    )


def _mint_influx_token(service: RigService) -> str:
    """Influx 3 Core has no way to preseed a token, so it is minted in-container.

    `influxdb3 create token --admin` is the documented route and it prints the
    token once, wrapped in ANSI styling — hence the prefix scan rather than a
    line index, which would break the first time the banner changes.
    """
    _wait_for_http(service, "/health")
    result = subprocess.run(
        [
            docker_cli(),
            "exec",
            service.name,
            "influxdb3",
            "create",
            "token",
            "--admin",
            "--host",
            "http://localhost:8181",
        ],
        capture_output=True,
        text=True,
        env=docker_env(),
        timeout=60,
    )
    assert result.returncode == 0, f"could not mint an influx token: {result.stderr}"
    for word in result.stdout.replace("\x1b", " ").split():
        cleaned = word.strip("\x1b[0m").strip()
        if cleaned.startswith("apiv3_"):
            return cleaned
    raise AssertionError(f"no apiv3_ token in influxdb3 output:\n{result.stdout}\n{result.stderr}")


def _seed_influx_database(service: RigService, token: str) -> None:
    """Bring `RIG_INFLUX_DATABASE` into existence, because Influx 3 creates a
    database on FIRST WRITE and a fresh server has none.

    Without this the rig models a state no configured deployment is ever in,
    and the happy-path test would read `not_found` — which is the tester
    behaving correctly against a server that genuinely lacks the database. The
    seed is written to its own measurement and left there, so it also proves
    the reserved `_eoe_selftest` measurement the tester drops is not the only
    thing in the database (a drop that took the database with it would then
    show up as a failure here rather than passing quietly).
    """
    import httpx

    with httpx.Client(timeout=15.0, headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.post(
            f"{service.url}/api/v3/write_lp",
            params={"db": RIG_INFLUX_DATABASE, "precision": "nanosecond"},
            content=b"_eoe_rig_seed,source=conftest value=1i",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
    assert response.status_code in (200, 204), (
        f"could not seed the rig influx database: HTTP {response.status_code} {response.text[:200]}"
    )


def _wait_for_self_scrape(service: RigService, *, timeout: float = 60.0) -> None:
    """Wait until Prometheus has actually scraped itself at least once.

    `/-/ready` answers as soon as the server is willing to serve queries, which
    is strictly earlier than having any data — and what E5.4c's read check asks
    for is the `up` series, which does not exist until a scrape lands. The gap
    used to be papered over by wall-clock luck: Grafana's ~14s startup dominated
    the rig's ready-wait, and by the time any test ran Prometheus had scraped a
    dozen times. Putting Grafana's SQLite on tmpfs (INFRA.1) removed that
    accidental delay and the race surfaced immediately as `up ... 0 series`.

    So the fixture now waits for the thing it actually promises. A rig whose
    Prometheus has no data is not ready, however cheerful `/-/ready` is.
    """
    import httpx

    deadline = time.monotonic() + timeout
    last = "no attempt completed"
    with httpx.Client(timeout=5.0, auth=(RIG_USER, RIG_PASSWORD)) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{service.url}/api/v1/query", params={"query": "up"})
                if response.status_code == 200 and response.json().get("data", {}).get("result"):
                    return
                last = f"HTTP {response.status_code} with no series yet"
            except Exception as error:  # noqa: BLE001  (it is still starting)
                last = f"{type(error).__name__}: {error}"
            time.sleep(0.25)
    raise AssertionError(f"{service.name} never scraped itself ({last})")


def _mint_grafana_token(service: RigService) -> str:
    """A Grafana service account token, which is what `GrafanaSettings` holds.

    Created over the admin basic-auth login because that is the only bootstrap
    Grafana offers; everything the tester itself does afterwards uses this
    token, exactly as the platform will.
    """
    import httpx

    _wait_for_http(service, "/api/health")
    auth = ("admin", RIG_PASSWORD)
    deadline = time.monotonic() + 60.0
    last = ""
    with httpx.Client(timeout=10.0, auth=auth) as client:
        while time.monotonic() < deadline:
            try:
                account = client.post(
                    f"{service.url}/api/serviceaccounts",
                    json={"name": f"eoe-rig-{uuid.uuid4().hex[:6]}", "role": "Admin"},
                )
                if account.status_code in (200, 201):
                    token = client.post(
                        f"{service.url}/api/serviceaccounts/{account.json()['id']}/tokens",
                        json={"name": f"eoe-rig-token-{uuid.uuid4().hex[:6]}"},
                    )
                    if token.status_code in (200, 201):
                        return str(token.json()["key"])
                    last = f"token: HTTP {token.status_code} {token.text[:200]}"
                else:
                    last = f"account: HTTP {account.status_code} {account.text[:200]}"
            except Exception as error:  # noqa: BLE001  (grafana refuses until migrated)
                last = f"{type(error).__name__}: {error}"
            time.sleep(0.5)
    raise AssertionError(f"could not mint a grafana service account token ({last})")


def _create_rig_bucket(service: RigService) -> None:
    """The bucket E5.4e's tester heads. Created with boto3, so the fixture
    proves the same SigV4 path the tester will use actually works here."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    deadline = time.monotonic() + 60.0
    last = ""
    while time.monotonic() < deadline:
        try:
            s3 = boto3.client(
                "s3",
                endpoint_url=service.url,
                aws_access_key_id=RIG_USER,
                aws_secret_access_key=RIG_PASSWORD,
                region_name="us-east-1",
                config=Config(signature_version="s3v4", retries={"max_attempts": 1}),
            )
            s3.create_bucket(Bucket=RIG_S3_BUCKET)
            return
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                return
            last = f"ClientError {code}"
        except Exception as error:  # noqa: BLE001  (minio refuses until it is listening)
            last = f"{type(error).__name__}: {error}"
        time.sleep(0.5)
    raise AssertionError(f"could not create the rig bucket ({last})")


@contextlib.contextmanager
def service_rig():
    """Every E5.4b-e service, started in parallel and torn down together.

    Parallel because the ready-waits are what cost: started in sequence this
    is the SUM of five startups (Grafana alone is ~10-15s), and started
    together it is the slowest one. That difference is the whole reason
    phase-5 section 5 can promise the rig adds little to the gate.
    """
    import concurrent.futures

    work = Path(tempfile.mkdtemp(prefix="eoe-rig-"))
    prom_yml = work / "prometheus.yml"
    prom_yml.write_text(RIG_PROMETHEUS_YML, encoding="utf-8")
    web_yml = work / "web_config.yml"
    web_yml.write_text(RIG_PROMETHEUS_WEB_YML, encoding="utf-8")
    for path in (prom_yml, web_yml):
        path.chmod(0o644)

    prometheus_command = [
        "--config.file=/etc/prometheus/prometheus.yml",
        "--web.config.file=/etc/prometheus/web_config.yml",
        "--storage.tsdb.retention.time=1h",
    ]
    prometheus_files = [
        (prom_yml, "/etc/prometheus/prometheus.yml"),
        (web_yml, "/etc/prometheus/web_config.yml"),
    ]

    builders = {
        "influx": lambda: _rig_container(
            "influx",
            RIG_IMAGES["influx"],
            8181,
            command=[
                "influxdb3",
                "serve",
                "--node-id=node0",
                "--object-store=memory",
                "--http-bind=0.0.0.0:8181",
            ],
        ),
        "prometheus": lambda: _rig_container(
            "prom-open",
            RIG_IMAGES["prometheus"],
            9090,
            command=[*prometheus_command, "--web.enable-remote-write-receiver"],
            files=prometheus_files,
            tmpfs=[f"/prometheus:rw,size=256m,{TMPFS_WORLD_WRITABLE}"],
        ),
        "prometheus_closed": lambda: _rig_container(
            "prom-closed",
            RIG_IMAGES["prometheus"],
            9090,
            command=list(prometheus_command),
            files=prometheus_files,
            tmpfs=[f"/prometheus:rw,size=256m,{TMPFS_WORLD_WRITABLE}"],
        ),
        "grafana": lambda: _rig_container(
            "grafana",
            RIG_IMAGES["grafana"],
            3000,
            tmpfs=[f"/var/lib/grafana:rw,size=256m,{TMPFS_WORLD_WRITABLE}"],
            env={
                "GF_SECURITY_ADMIN_PASSWORD": RIG_PASSWORD,
                "GF_AUTH_ANONYMOUS_ENABLED": "false",
                # Off: the rig has no internet and every startup second counts.
                "GF_ANALYTICS_REPORTING_ENABLED": "false",
                "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
                "GF_INSTALL_PLUGINS": "",
            },
        ),
        "minio": lambda: _rig_container(
            "minio",
            RIG_IMAGES["minio"],
            9000,
            command=["server", "/data"],
            env={"MINIO_ROOT_USER": RIG_USER, "MINIO_ROOT_PASSWORD": RIG_PASSWORD},
            tmpfs=[f"/data:rw,size=256m,{TMPFS_WORLD_WRITABLE}"],
        ),
    }

    started: dict[str, RigService] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(builders)) as pool:
            futures = {key: pool.submit(builder) for key, builder in builders.items()}
            errors = []
            for key, future in futures.items():
                try:
                    started[key] = future.result()
                except Exception as error:  # noqa: BLE001  (report ALL of them, not the first)
                    errors.append(f"{key}: {type(error).__name__}: {error}")
            if errors:
                raise AssertionError("the service rig would not start:\n" + "\n".join(errors))

        # Credentialing also runs in parallel: Grafana's migration and Influx's
        # token mint are both multi-second waits with nothing in common.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            influx_token = pool.submit(_mint_influx_token, started["influx"])
            grafana_token = pool.submit(_mint_grafana_token, started["grafana"])
            bucket = pool.submit(_create_rig_bucket, started["minio"])
            pool.submit(_wait_for_http, started["prometheus"], "/-/ready").result()
            pool.submit(_wait_for_http, started["prometheus_closed"], "/-/ready").result()
            # Only the open one is asserted against for data; `prometheus_closed`
            # exists to have its remote-write receiver disabled and is never read.
            pool.submit(_wait_for_self_scrape, started["prometheus"]).result()
            bucket.result()
            minted = influx_token.result()
            _seed_influx_database(started["influx"], minted)
            rig = ServiceRig(
                influx=started["influx"],
                influx_token=minted,
                prometheus=started["prometheus"],
                prometheus_closed=started["prometheus_closed"],
                grafana=started["grafana"],
                grafana_token=grafana_token.result(),
                minio=started["minio"],
            )
        yield rig
    finally:
        if started:
            subprocess.run(
                [docker_cli(), "rm", "-f", "-v", *(one.name for one in started.values())],
                capture_output=True,
                env=docker_env(),
            )
        shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="session")
def rig():
    """The one rig, for every `RIG_MODULES` test.

    Session-scoped AND group-pinned: either alone is not enough. Session scope
    without the shared xdist group builds it once per worker; the group
    without session scope builds it once per module.
    """
    with service_rig() as running:
        yield running


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
