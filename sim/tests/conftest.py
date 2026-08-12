"""Sim's test configuration (tasks SIM.1 to SIM.5).

Four jobs, and the first is not to grow a second copy of anything.

**The fixtures are the backend's.** `backend/tests/conftest.py` owns the only
Mosquitto fixture in this repository and it stays that way (phase doc section
2): `ephemeral_broker`, `ephemeral_postgres`, `free_port`, `make_kek` and
`docker_retry` are loaded from it and re-exported here, so a sim test writes
`from conftest import ephemeral_broker` exactly as a backend test does. A
second broker fixture would drift from the first the day one of them learned
something about Docker Desktop that the other did not.

**The D99 parallel conventions are copied, because they have to be.** Every
live test here starts its own Postgres and Mosquitto; splitting one module
across xdist workers would start those containers once per worker and make the
suite slower rather than faster. See the hook below for why `tryfirst` is
load-bearing.

**The platform under test lives here too** (SIM.2). Several test modules need a
migrated database, a provisioned TLS broker and a running API, and the `platform`
fixture below is module-scoped: each module pays for its own containers exactly
once and no module can leave state in another's. It was SIM.1's, inside
`test_mock_aggregator.py`, until there were three callers — which is one more
than a copy survives.

**And a platform that answers real HTTP** (SIM.4). `platform_stack` is the shared
body; `platform` is it in-process, and `live_stack` plus `uvicorn_server` is it on
a socket with the reconciliation worker inside, which is what a REST provisioner
and a one-command fleet runner have to be tested against.

**R0 belongs to this suite too** (SIM.5). `pytest_sessionfinish` below says so
loudly; `tests/gate_runner.py` is what makes it a failing exit.
"""

import asyncio
import contextlib
import importlib.util
import os
import sys
import threading
import time
import types
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from app.auth.passwords import hash_password
from app.controlplane.revision_state import RevisionState
from app.controlplane.runner import ReconciliationWorker
from app.db import create_session_factory
from app.devbroker import device_username
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AggregatorStatus,
    ConfigRevision,
    Deployment,
    DeviceState,
    Pod,
    RoleAssignment,
    User,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy
from app.settings import Settings
from fastapi.testclient import TestClient
from sqlalchemy import select

from device import BrokerLogin
from provision import mint_credentials

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
BACKEND_TESTS = BACKEND / "tests"


def _load_backend_conftest() -> types.ModuleType:
    """Load `backend/tests/conftest.py` under a name of its OWN.

    A plain `import conftest` cannot work from here: pytest has already
    registered THIS file as the module named `conftest`, so the import would
    hand back this half-built module and fail on the first name taken out of
    it. Loading the backend's file from its path under an explicit alias
    sidesteps the collision entirely.

    Importing it also runs its module body, which registers and loads the
    derandomized hypothesis profile (D-gate determinism, backend conftest) —
    which is what keeps the checksum cross-check green or red for a reason
    rather than by luck.

    `backend/tests` goes on `sys.path` as well, so a sim test can reach the
    backend's other shared test helpers the same way its own suites do.
    """
    if str(BACKEND_TESTS) not in sys.path:
        sys.path.insert(0, str(BACKEND_TESTS))
    spec = importlib.util.spec_from_file_location(
        "eoe_backend_conftest", BACKEND_TESTS / "conftest.py"
    )
    assert spec is not None and spec.loader is not None, f"no conftest at {BACKEND_TESTS}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_backend = _load_backend_conftest()

# One broker fixture, one Postgres fixture, one repository.
docker_retry = _backend.docker_retry
ephemeral_broker = _backend.ephemeral_broker
ephemeral_postgres = _backend.ephemeral_postgres
free_port = _backend.free_port
make_kek = _backend.make_kek


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items) -> None:
    """Pin every test to an xdist group named after its module (D99).

    **`tryfirst` is load-bearing.** xdist reads the `xdist_group` mark inside
    its OWN `pytest_collection_modifyitems` and bakes the group into the
    nodeid there; a mark added after that hook has run is never seen, and
    every test scatters across workers as if unmarked. The failure is silent —
    the suite still runs, it just runs wrong — and it cost the backend a red
    gate to find.
    """
    for item in items:
        module = item.module.__name__.rsplit(".", 1)[-1] if item.module else "orphan"
        item.add_marker(pytest.mark.xdist_group(module))


def pytest_sessionfinish(session, exitstatus) -> None:
    """Say loudly when a run under EOE_GATE skipped, xfailed or deselected (R0).

    SIM.5's half of R0 for this suite, and the backend's hook verbatim in
    behaviour for a reason: the ENFORCEMENT (the nonzero exit) lives in
    `tests/gate_runner.py`, because pytest 9 ignores exitstatus mutation in this
    hook (D11). This is the loud line, so a plain `uv run pytest` surfaces the
    violation too. Filtered runs stay legal while debugging between gates; they
    are only forbidden as a way to CLEAR a gate.
    """
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


@pytest.fixture
def anyio_backend() -> str:
    """The harness is asyncio (aiomqtt, the phase's fixed client choice), so
    async tests run on anyio's plugin pinned to the one backend the code
    actually uses. Parametrizing over trio as well would double the suite for
    a runtime nothing here will ever run on."""
    return "asyncio"


# ===========================================================================
# A whole platform, for the modules that assert against one
# ===========================================================================

#: The seeded demo hierarchy (`app.seed`) is the fleet under test; SIM.4 is
#: where a harness provisions its own over the REST API.
DEP = "redwood-coast"
AGG = "demo-agg-rc-01"
#: A second Aggregator in the SAME deployment, for the scenarios that need one
#: device to say something about another's Listener.
OTHER_AGG = "demo-agg-rc-02"
#: The first Listener of `AGG`, by the demo fixture's deterministic MACs.
MAC = "02:EE:0E:01:01:01"
SECOND_MAC = "02:EE:0E:01:01:02"

#: An Aggregator that exists at provisioning time — so `app.devbroker` mints it
#: a real credential — and is deleted from inventory by the test that needs a
#: device the platform has never heard of. That order is the only honest one:
#: broker accounts come from inventory, so an unprovisioned reporter can only
#: be hardware inventory has FORGOTTEN, which is exactly the field story spec
#: 4.3 item 3 is about.
GHOST_AGG = "sim-ghost-01"
GHOST_POD = "Pod SIM · Ghost"

OWNER = "sim-owner@example.com"
PASSWORD = f"sim-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Platform:
    """Everything a device needs to be talked to, and everything a test needs
    to check what the platform made of it.

    `database_url`, `kek` and `certs` were added by SIM.4: a fleet is
    provisioned into inventory FIRST and given broker credentials SECOND (that
    is the only order accounts can be minted in, since they come from inventory),
    so a test has to be able to re-run the generator against this stack mid-run.
    """

    app: Any
    broker: Any
    factory: Any
    store: SecretStore
    manifest: dict[str, Any]
    database_url: str
    kek: str
    certs: Path


def mint_broker_credentials(
    url: str, kek: str, out_dir: Path, *, host: str = "localhost", port: int = 8883
) -> dict[str, Any]:
    """Mint TLS material, broker accounts, ACLs and the `deployment_service`
    rows by running the platform's own generator — the same command the README
    gives an operator. The harness never writes broker credentials itself.

    Routed through `provision.mint_credentials` (SIM.4) rather than through a
    second `subprocess.run` written here, so the code path the fleet runner uses
    in anger is the one every live module in this suite pays for on the way up.
    `command`/`cwd` override its default `uv run`: there is no uv inside a
    pytest process, and this interpreter already has the backend importable.
    """
    return mint_credentials(
        out_dir,
        database_url=url,
        kek=kek,
        host=host,
        port=port,
        command=[sys.executable, "-m", "app.devbroker"],
        cwd=BACKEND,
        timeout=180,
    )


@contextlib.contextmanager
def platform_stack(out: Path, port: int, **settings: Any):
    """A migrated database, a provisioned TLS broker, and an API whose lifespan
    has actually run, so `app.state.mqtt` is the real outbound manager.

    A context manager as well as a fixture because SIM.4 needs the same stack
    with different `Settings` — one that also runs the reconciliation worker
    in-process — and two copies of this body would drift the day one of them
    learned something about broker provisioning that the other did not.
    """
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            deployment_id = db.scalars(select(Deployment.id).where(Deployment.slug == DEP)).one()
            pod = Pod(deployment_id=deployment_id, name=GHOST_POD, tags=[])
            db.add(pod)
            db.flush()
            db.add(Aggregator(pod_id=pod.id, aggregator_uuid=GHOST_AGG))
            db.commit()
        # `localhost` and the host port this broker will publish on, written
        # onto the rows by the generator itself rather than patched in
        # afterwards: the suite reaches the same broker the device does — one
        # broker, two clients, no shortcuts.
        manifest = mint_broker_credentials(url, kek, out, host="localhost", port=port)
        with ephemeral_broker(out, host_port=port) as broker:
            app = create_app(
                Settings(
                    database_url=url,
                    # The SAME kek the broker secrets were wrapped with: a fresh
                    # one reads as "every broker row is unreadable", which the
                    # manager survives by design and which would make every
                    # test quietly prove nothing.
                    kek=kek,
                    session_secret="sim-session-secret",
                    cors_origins="",
                    **settings,
                )
            )
            with app.state.session_factory() as db:
                user = User(email=OWNER, password_hash=hash_password(PASSWORD))
                user.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
                db.add(user)
                db.commit()
            yield Platform(
                app=app,
                broker=broker,
                factory=factory,
                store=SecretStore(factory, kek),
                manifest=manifest,
                database_url=url,
                kek=kek,
                certs=out,
            )


@pytest.fixture(scope="module")
def platform(tmp_path_factory):
    out = tmp_path_factory.mktemp("sim-certs")
    with platform_stack(out, free_port()) as running:
        yield running


@contextlib.contextmanager
def uvicorn_server(app: Any, *, host: str = "127.0.0.1"):
    """Serve `app` on a real socket for the life of the block.

    A thread rather than a subprocess, because the app object is already built
    here — with the ephemeral database's URL and the KEK the broker secrets were
    wrapped with — and rebuilding it from environment variables in a child
    process would be a second configuration path to get wrong.
    """
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name=f"sim-api-{port}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 120
    while not server.started:
        assert thread.is_alive(), "the API server thread died before it started serving"
        assert time.monotonic() < deadline, f"the API never started serving on {host}:{port}"
        time.sleep(0.05)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=120)
        assert not thread.is_alive(), "the API server thread would not shut down"


@pytest.fixture(scope="module")
def live_stack(tmp_path_factory):
    """The whole platform in ONE process — API, publisher, worker and consumer —
    with no HTTP server started yet (SIM.4).

    Two things the in-process `platform` fixture cannot offer, and SIM.4 needs
    both. `provision.py` is a REST client built on `urllib`, so it needs a socket
    rather than an ASGI transport: provisioning "as an operator would" means
    little if the operator is a test client. And `fleet.py`'s acceptance is that
    ONE command converges a fleet, which means the publisher and the
    reconciliation consumer must be running without a test holding them open —
    `worker_in_api=True` is how the platform itself ships that (E3.7's
    single-container deployment), so it is what the acceptance runs against.

    The SERVER is deliberately left to the caller, because a fleet's provisioning
    sequence needs two of them: broker accounts are minted from inventory, so
    inventory is created over REST first, and re-running the generator rotates
    every password — including the platform's own — which drops the API's live
    broker sessions. `MqttClientManager.start()` reads its coordinates once
    (E3.2, deliberately), so the API has to come back up before it can publish
    again. That is the operator's real sequence too, and `test_fleet.py` performs
    it rather than working around it.

    The sweep intervals are pushed past the life of the module for `worker_for`'s
    reason — a pending revision timing out mid-test would be a failure about
    patience rather than about the fleet — and the consumer, being
    subscription-driven rather than swept, is unaffected.
    """
    out = tmp_path_factory.mktemp("sim-live-certs")
    with platform_stack(
        out,
        free_port(),
        worker_in_api=True,
        timeout_sweep_seconds=3600,
        drift_sweep_seconds=3600,
    ) as running:
        yield running


# --- talking to the platform as an operator ---------------------------------


def operator(app) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


async def apply_config(
    client: TestClient,
    *,
    entity_type: str,
    ids: list[str],
    changes: dict[str, Any],
    level: str = "target",
) -> list[dict[str, Any]]:
    """Edit config the way the UI does, and publish. Returns every revision the
    apply created — an aggregator-level change moves its Listeners' effective
    config too, so E2 cuts one revision per affected device and all go out."""
    body = {
        "selection": {"entity_type": entity_type, "where": {"ids": ids}},
        "changes": changes,
        "level": level,
    }
    response = await asyncio.to_thread(
        client.post, f"{API_PREFIX}/config/apply", json=body, headers=csrf(client)
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["published"] == len(result["revisions"]), "apply did not publish"
    revisions: list[dict[str, Any]] = result["revisions"]
    return revisions


async def apply_change(client: TestClient, aggregator_id: uuid.UUID, verbosity: str) -> uuid.UUID:
    """One aggregator-level `logging.verbosity` change; the Aggregator's own
    revision id comes back. `logging.verbosity` applies at every level, so
    this is also the expensive shape: every Listener under the Aggregator gets
    a revision too."""
    revisions = await apply_config(
        client,
        entity_type="aggregator",
        ids=[str(aggregator_id)],
        changes={"logging.verbosity": verbosity},
    )
    return uuid.UUID(next(r for r in revisions if r["target_type"] == "aggregator")["revision_id"])


def worker_for(platform: Platform) -> ReconciliationWorker:
    """The real spec 6.4 loop. The sweep intervals are pushed past the life of
    a test on purpose: a pending revision timing out mid-test would be a
    failure about patience rather than about the device."""
    return ReconciliationWorker(
        platform.factory, platform.store, timeout_interval=3600, drift_interval=3600
    )


# --- reading the platform's mind --------------------------------------------


def device_login(platform: Platform, aggregator_uuid: str = AGG) -> BrokerLogin:
    """The device's OWN credential, read from `accounts.json` — the one place
    dev broker passwords live. Its ACL grants seven topics and no more, so
    every test runs against the spec 7.1 isolation as well."""
    username = device_username(aggregator_uuid)
    password = next(
        account["password"]
        for account in platform.manifest["accounts"]
        if account["username"] == username
    )
    return BrokerLogin(
        host="localhost",
        port=platform.broker.port,
        username=username,
        password=password,
        ca_cert=Path(platform.manifest["ca_cert"]),
    )


def deployment_id_of(factory, slug: str = DEP) -> uuid.UUID:
    with factory() as db:
        return db.scalars(select(Deployment.id).where(Deployment.slug == slug)).one()


def aggregator_id_of(factory, aggregator_uuid: str = AGG) -> uuid.UUID:
    with factory() as db:
        return db.scalars(
            select(Aggregator.id).where(Aggregator.aggregator_uuid == aggregator_uuid)
        ).one()


def revision(factory, revision_id: uuid.UUID) -> tuple[str, str]:
    with factory() as db:
        row = db.execute(
            select(ConfigRevision.state, ConfigRevision.checksum).where(
                ConfigRevision.id == revision_id
            )
        ).one()
        return row.state, row.checksum


def status_row(factory, aggregator_uuid: str = AGG) -> AggregatorStatus | None:
    with factory() as db:
        return db.scalars(
            select(AggregatorStatus).where(
                AggregatorStatus.aggregator_id == aggregator_id_of(factory, aggregator_uuid)
            )
        ).first()


def listener_state(factory, mac: str) -> DeviceState | None:
    """One Listener's stored reported state (spec 6.1), or None if the platform
    has never accepted a report for it."""
    with factory() as db:
        return db.scalars(
            select(DeviceState).where(
                DeviceState.entity_type == "listener", DeviceState.entity_id == mac
            )
        ).first()


async def eventually(probe, *, what: str, timeout: float = 30.0, interval: float = 0.1):
    """Poll the platform until `probe()` returns something truthy.

    Tests wait on the PLATFORM here and on the DEVICE through its own waiters,
    which is the split that keeps this suite honest: a sleep long enough to
    pass on a fast host is a sleep that fails on a loaded one, and a poll
    against the database is the only way to observe a consumer that has no
    callback to offer. The message names what never happened.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        found = await asyncio.to_thread(probe)
        if found:
            return found
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"the platform never {what} within {timeout}s")
        await asyncio.sleep(interval)


async def wait_for_state(
    factory, revision_id: uuid.UUID, wanted: RevisionState, *, timeout: float = 30.0
) -> None:
    try:
        await eventually(
            lambda: revision(factory, revision_id)[0] == wanted.value,
            what=f"moved revision {revision_id} to {wanted.value}",
            timeout=timeout,
        )
    except AssertionError as error:
        # Re-raised with the state it actually reached: "never became applied"
        # and "became failed instead" are different bugs, and reading the row
        # only on failure keeps the poll to one query per pass.
        raise AssertionError(f"{error}; it is {revision(factory, revision_id)[0]}") from None


async def wait_for_verdict(
    factory, *, online: bool, aggregator_uuid: str = AGG, timeout: float = 30.0
) -> AggregatorStatus:
    def verdict() -> AggregatorStatus | None:
        row = status_row(factory, aggregator_uuid)
        return row if row is not None and row.online is online else None

    return await eventually(
        verdict, what=f"recorded {aggregator_uuid} online={online}", timeout=timeout
    )
