"""E5.7b: `MqttClientManager.refresh()` and the service-config sweep.

The four acceptance criteria from the phase document. Three of them are about a
REAL broker and are written that way, because the property under test is
"a connection exists and carries messages", and a mock manager can be made to
report that without any of it being true.

1. **A deployment whose `mqtt` row is created AFTER the worker started acquires
   a live connection within one refresh interval**, with no process restart.
2. **Rotating a broker password reconnects that deployment and leaves other
   deployments' connections unbroken** — asserted by holding a subscription on
   a second deployment across the rotation and seeing no gap.
3. **A cold-start Aggregator created after the services were configured finds a
   retained desired message carrying all twelve keys waiting at the broker**,
   asserted by subscribing AFTER the publish. Subscribing first would pass on
   an ordinary publish and prove nothing about retention, which is the whole
   spec 6.4 property.
4. **`refresh()` is idempotent** — called with unchanged coordinates it cancels
   no task, asserted by IDENTITY on the task objects rather than by counting
   them, because a stop/start cycle keeps the count and drops every message in
   between.
"""

import asyncio
import json
import subprocess
import uuid

import pytest
from conftest import (
    docker_cli,
    docker_env,
    ephemeral_broker,
    ephemeral_postgres,
    free_port,
    make_kek,
)

from app.config.overrides import get_overrides
from app.contracts.mqtt import QOS, aggregator_root, deployment_root
from app.controlplane.broker import (
    MqttClientManager,
    load_broker_coordinates,
)
from app.controlplane.publisher import publish_all
from app.db import create_session_factory
from app.devbroker import Account, platform_username, write_artifacts
from app.models import (
    Aggregator,
    ConfigRevision,
    Deployment,
    DeploymentService,
    Organization,
    Pod,
)
from app.secrets import SecretStore
from app.services.clients.mqtt import MqttServiceClient
from app.services.config_sweep import service_config_sweep
from app.services.store import upsert_service

pytestmark = pytest.mark.anyio

SLUG_A = "e57b-alpha"
SLUG_B = "e57b-beta"
PW_A = "e57b-alpha-account"
PW_B = "e57b-beta-account"
PW_A2 = "e57b-alpha-rotated"


# --- A broker serving two deployments ----------------------------------------


@pytest.fixture(scope="module")
def certs_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("e57b-dev-certs")


def accounts(alpha_password: str) -> list[Account]:
    """Both deployments' platform accounts. Regenerating the password file with
    a different alpha password is how the rotation test rotates."""
    return [
        Account(
            username=platform_username(SLUG_A),
            password=alpha_password,
            kind="platform",
            deployment_slug=SLUG_A,
        ),
        Account(
            username=platform_username(SLUG_B),
            password=PW_B,
            kind="platform",
            deployment_slug=SLUG_B,
        ),
    ]


@pytest.fixture(scope="module")
def broker(certs_dir):
    """One dev broker holding BOTH deployments' accounts, on a fixed host port.

    A fixed port (`free_port`) rather than a Docker-assigned one because the
    rotation test rewrites the password file and SIGHUPs the container, and the
    coordinates stored in the database have to stay valid across that.
    """
    write_artifacts(certs_dir, accounts(PW_A), host="127.0.0.1", port=8883, regenerate_tls=True)
    with ephemeral_broker(certs_dir, host_port=free_port()) as running:
        yield running


def rewrite_broker_accounts(certs_dir, broker, alpha_password: str) -> None:
    """Regenerate the password file and put it INSIDE the running container.

    `ephemeral_broker` ships files in with `docker cp` rather than a bind mount
    (a deliberate cross-platform choice, INTERFACES "The development broker"),
    so rewriting the host directory changes nothing the broker can see. This
    caught a genuine test defect: without the copy, the "rotation" only
    rotated the database's copy of the password and the assertion that alpha
    reconnected was passing for the wrong reason.
    """
    write_artifacts(
        certs_dir, accounts(alpha_password), host="127.0.0.1", port=8883, regenerate_tls=False
    )
    copied = subprocess.run(
        [docker_cli(), "cp", str(certs_dir / "passwd"), f"{broker.name}:/mosquitto/dev/passwd"],
        capture_output=True,
        text=True,
        env=docker_env(),
    )
    assert copied.returncode == 0, copied.stderr
    broker.reload()


@pytest.fixture(scope="module")
def pg_url():
    with ephemeral_postgres() as url:
        yield url


@pytest.fixture(scope="module")
def factory(pg_url):
    _, session_factory = create_session_factory(pg_url)
    return session_factory


@pytest.fixture(scope="module")
def store(factory):
    return SecretStore(factory, make_kek())


@pytest.fixture(scope="module")
def hierarchy(factory):
    """Two deployments; only ALPHA gets a broker row up front.

    Beta's row is what the "created after the worker started" acceptance
    creates, so it deliberately does not exist yet.
    """
    with factory() as db:
        org = Organization(name="e57b-org")
        db.add(org)
        db.flush()
        alpha = Deployment(organization_id=org.id, name="Alpha", slug=SLUG_A)
        beta = Deployment(organization_id=org.id, name="Beta", slug=SLUG_B)
        db.add_all([alpha, beta])
        db.flush()
        ids = {"alpha": alpha.id, "beta": beta.id}
        for key, deployment_id in ids.items():
            pod = Pod(deployment_id=deployment_id, name=f"e57b-{key}-pod")
            db.add(pod)
            db.flush()
            db.add(
                Aggregator(
                    pod_id=pod.id,
                    aggregator_uuid=f"e57b-{key}-agg",
                    name=f"e57b-{key}-agg",
                )
            )
        db.commit()
    return ids


def add_broker_row(
    factory, store, broker, deployment_id: uuid.UUID, slug: str, password: str
) -> None:
    """Create or rotate one deployment's `mqtt` service row."""
    name = f"deployment:{deployment_id}:mqtt_password"
    store.put(name, password)
    with factory() as db:
        upsert_service(
            db,
            deployment_id,
            "mqtt",
            config={},
            secret_names={},
            password_secret_name=name,
            host="127.0.0.1",
            port=broker.port,
            tls_enabled=True,
            ca_cert_pem=(broker.dev_dir / "ca.crt").read_text(encoding="utf-8"),
            username=platform_username(slug),
        )
        db.commit()


@pytest.fixture
def manager(factory, store, hierarchy, broker):
    """A manager holding ALPHA only, started and connected."""
    add_broker_row(factory, store, broker, hierarchy["alpha"], SLUG_A, PW_A)
    return MqttClientManager(lambda: load_broker_coordinates(factory, store))


@pytest.fixture(autouse=True)
def reset(factory, store, hierarchy, broker, certs_dir):
    """Put the world back: alpha's row and password as they started, beta's
    row gone, and the broker's password file regenerated to match."""
    yield
    from sqlalchemy import delete as sql_delete

    from app.models import EntityOverride

    with factory() as db:
        db.execute(sql_delete(ConfigRevision))
        # The projection lives in `entity_override`; leaving it behind would
        # make the next test's first sweep a no-op for reasons that have
        # nothing to do with what it is testing.
        db.execute(sql_delete(EntityOverride))
        db.execute(sql_delete(DeploymentService).where(DeploymentService.service_key != "mqtt"))
        db.execute(
            sql_delete(DeploymentService).where(
                DeploymentService.deployment_id == hierarchy["beta"]
            )
        )
        db.commit()
    add_broker_row(factory, store, broker, hierarchy["alpha"], SLUG_A, PW_A)
    rewrite_broker_accounts(certs_dir, broker, PW_A)


# --- 1: a deployment configured after start, with no restart -----------------


async def test_a_broker_row_created_after_start_gets_a_live_connection(
    manager, factory, store, hierarchy, broker
) -> None:
    async with manager:
        await manager.wait_connected(hierarchy["alpha"], timeout=20)
        assert manager.deployment_ids == (hierarchy["alpha"],)

        # The operator finishes configuring the second deployment's broker.
        add_broker_row(factory, store, broker, hierarchy["beta"], SLUG_B, PW_B)
        started, stopped, restarted = await manager.refresh()
        assert (started, stopped, restarted) == (1, 0, 0)

        # Live, not merely tracked: `wait_connected` returns only after the
        # subscription set has been replayed onto the new connection.
        await manager.wait_connected(hierarchy["beta"], timeout=20)
        assert manager.is_connected(hierarchy["beta"])
        # And it can actually carry a message.
        await manager.publish(
            hierarchy["beta"], f"{deployment_root(SLUG_B)}/_selftest", b"hello", qos=QOS
        )


async def test_a_removed_broker_row_stops_that_deployments_connection(
    manager, factory, store, hierarchy, broker
) -> None:
    from sqlalchemy import delete as sql_delete

    async with manager:
        add_broker_row(factory, store, broker, hierarchy["beta"], SLUG_B, PW_B)
        await manager.refresh()
        await manager.wait_connected(hierarchy["beta"], timeout=20)

        with factory() as db:
            db.execute(
                sql_delete(DeploymentService).where(
                    DeploymentService.deployment_id == hierarchy["beta"]
                )
            )
            db.commit()
        started, stopped, restarted = await manager.refresh()
        assert (started, stopped, restarted) == (0, 1, 0)
        assert hierarchy["beta"] not in manager.deployment_ids
        assert not manager.is_connected(hierarchy["beta"])


# --- 2: rotation reconnects one deployment and not the others ----------------


async def test_rotating_a_password_reconnects_only_that_deployment(
    manager, factory, store, hierarchy, broker, certs_dir
) -> None:
    """**The gap assertion is the point.**

    A subscription held on BETA across ALPHA's rotation must keep delivering.
    Asserted by publishing to beta after the refresh and reading it back on a
    subscription opened BEFORE the refresh — if beta's task had been cancelled
    and restarted, the subscription would have gone with it.
    """
    async with manager:
        await manager.wait_connected(hierarchy["alpha"], timeout=20)
        add_broker_row(factory, store, broker, hierarchy["beta"], SLUG_B, PW_B)
        await manager.refresh()
        await manager.wait_connected(hierarchy["beta"], timeout=20)

        beta_task = manager._tasks[hierarchy["beta"]]
        alpha_task = manager._tasks[hierarchy["alpha"]]

        # An independent subscriber on beta's namespace, opened BEFORE the
        # rotation and held across it.
        watcher = MqttServiceClient(
            deployment_id=hierarchy["beta"],
            deployment_slug=SLUG_B,
            host="127.0.0.1",
            port=broker.port,
            username=platform_username(SLUG_B),
            password=PW_B,
            tls_enabled=True,
            ca_cert_pem=(broker.dev_dir / "ca.crt").read_text(encoding="utf-8"),
        )
        topic = f"{deployment_root(SLUG_B)}/_selftest"
        async with watcher.connect() as subscriber:
            await subscriber.subscribe(topic, qos=QOS)

            # Rotate alpha's password on the broker AND in the row.
            rewrite_broker_accounts(certs_dir, broker, PW_A2)
            add_broker_row(factory, store, broker, hierarchy["alpha"], SLUG_A, PW_A2)

            started, stopped, restarted = await manager.refresh()
            assert (started, stopped, restarted) == (0, 0, 1), (
                "a rotated password IS a difference; `BrokerCoordinates` is a frozen "
                "dataclass so equality compares it"
            )

            # Alpha's task was replaced; beta's is the SAME OBJECT.
            assert manager._tasks[hierarchy["alpha"]] is not alpha_task
            assert manager._tasks[hierarchy["beta"]] is beta_task

            # Alpha reconnects on the new password, without a process restart.
            await manager.wait_connected(hierarchy["alpha"], timeout=20)

            # And beta never dropped: its subscription still delivers.
            body = uuid.uuid4().hex.encode()
            await manager.publish(hierarchy["beta"], topic, body, qos=QOS)
            async with asyncio.timeout(10):
                async for message in subscriber.messages:
                    if message.topic.matches(topic) and bytes(message.payload) == body:
                        break


# --- 4: idempotence, asserted by identity ------------------------------------


async def test_refresh_with_unchanged_coordinates_cancels_nothing(manager, hierarchy) -> None:
    """Counting tasks would pass on a stop/start cycle that dropped every
    message in between. Identity is what actually says "untouched"."""
    async with manager:
        await manager.wait_connected(hierarchy["alpha"], timeout=20)
        before = dict(manager._tasks)
        for _ in range(3):
            assert await manager.refresh() == (0, 0, 0)
        assert dict(manager._tasks) == before
        for deployment_id, task in before.items():
            assert manager._tasks[deployment_id] is task
            assert not task.done()
        assert manager.is_connected(hierarchy["alpha"])


async def test_refreshing_a_manager_that_never_started_is_a_no_op(factory, store) -> None:
    """Both hosts poll on a timer while `start_or_retry` may still be backing
    off; a raise here would fill a log with a self-resolving state."""
    idle = MqttClientManager(lambda: load_broker_coordinates(factory, store))
    assert await idle.refresh() == (0, 0, 0)


# --- 3: a cold-start Aggregator finds retained desired config ----------------


async def test_a_late_aggregator_finds_the_twelve_keys_retained_at_the_broker(
    manager, factory, store, hierarchy, broker
) -> None:
    """**Spec 16.4, end to end, through the sweep that E5.7b registers.**

    The services are configured first and the Aggregator is created second,
    which is the order a growing deployment actually works in. The sweep is
    what closes the gap, and the assertion subscribes AFTER the publish so it
    is retention being tested rather than delivery.
    """
    projected_keys = {
        "telemetry.influx_url",
        "telemetry.influx_token",
        "telemetry.influx_database",
        "telemetry.grafana_url",
    }
    with factory() as db:
        for name, value in (
            (f"deployment:{hierarchy['alpha']}:influx_token", "late-influx-token"),
        ):
            store.put(name, value)
        upsert_service(
            db,
            hierarchy["alpha"],
            "influx",
            config={"url": "https://influx.example:8181", "database": "recordings"},
            secret_names={"token": f"deployment:{hierarchy['alpha']}:influx_token"},
            password_secret_name=None,
        )
        upsert_service(
            db,
            hierarchy["alpha"],
            "grafana",
            config={"base_url": "https://grafana.example:3000"},
            secret_names={},
            password_secret_name=None,
        )
        db.commit()

    # The device is created AFTER the services. Its own pod, so it is a new
    # Aggregator rather than a second one on an occupied pod.
    late_uuid = "e57b-late-agg"
    with factory() as db:
        pod = Pod(deployment_id=hierarchy["alpha"], name="e57b-late-pod")
        db.add(pod)
        db.flush()
        db.add(Aggregator(pod_id=pod.id, aggregator_uuid=late_uuid, name="late"))
        db.commit()

    async with manager:
        await manager.wait_connected(hierarchy["alpha"], timeout=20)
        report = await service_config_sweep(factory, store, manager, publish_enabled=True)
        assert report.failures == 0, report
        assert report.revisions >= 1, report
        assert report.published == report.revisions, report

    # **Subscribe AFTER the publish, and after the manager has gone away**: a
    # message that arrives now arrived because the broker RETAINED it, which is
    # the property spec 6.4 depends on for a device that was offline.
    topic = f"{aggregator_root(SLUG_A, late_uuid)}/desired"
    reader = MqttServiceClient(
        deployment_id=hierarchy["alpha"],
        deployment_slug=SLUG_A,
        host="127.0.0.1",
        port=broker.port,
        username=platform_username(SLUG_A),
        password=PW_A,
        tls_enabled=True,
        ca_cert_pem=(broker.dev_dir / "ca.crt").read_text(encoding="utf-8"),
    )
    async with reader.connect() as client:
        await client.subscribe(topic, qos=QOS)
        async with asyncio.timeout(15):
            async for message in client.messages:
                if not message.topic.matches(topic):
                    continue
                assert message.retain, "the desired topic must be RETAINED (spec 7.2)"
                payload = json.loads(bytes(message.payload))
                break
    assert projected_keys <= set(payload["config"]), payload["config"]
    # The eight non-secret keys arrive as values.
    assert payload["config"]["telemetry.influx_url"] == "https://influx.example:8181"
    assert payload["config"]["telemetry.grafana_url"] == "https://grafana.example:3000"
    # **The secret arrives as a D51 MARKER, not as plaintext**, and that is the
    # E3.4 contract rather than a gap in this delivery path: a revision
    # snapshot never holds a credential (spec 5.4, 8), `desired_payload` sends
    # `revision.snapshot` verbatim, and the plaintext reaches the device
    # through E4's provisioning bundle. Asserted here so E5's projection is
    # pinned to that boundary — if a later change ever put a credential in a
    # retained broker message, this is the test that fails.
    assert payload["config"]["telemetry.influx_token"] == {
        "$secret": f"config:deployment:{hierarchy['alpha']}:telemetry.influx_token"
    }
    assert "late-influx-token" not in json.dumps(payload)


async def test_the_sweep_is_a_no_op_the_second_time(
    manager, factory, store, hierarchy, broker
) -> None:
    """It runs on a timer, so "already up to date" has to write nothing.

    This is E5.7a's `changed_keys` fix seen from the sweep's side: without it,
    every pass would mint a revision for every device and republish the fleet
    once a minute.
    """
    with factory() as db:
        upsert_service(
            db,
            hierarchy["alpha"],
            "grafana",
            config={"base_url": "https://grafana.example:3000"},
            secret_names={},
            password_secret_name=None,
        )
        db.commit()

    async with manager:
        await manager.wait_connected(hierarchy["alpha"], timeout=20)
        first = await service_config_sweep(factory, store, manager, publish_enabled=True)
        assert first.revisions >= 1
        second = await service_config_sweep(factory, store, manager, publish_enabled=True)
    assert second.revisions == 0, second
    assert second.published == 0, second
    assert not second.changed


async def test_the_sweep_does_not_reset_the_credentials_generation(
    factory, store, hierarchy
) -> None:
    """D151: the sweep must DELIVER the rotation counter, never overwrite it.

    `service_settings` omits `services.credentials_generation` when no
    generation is passed, so the projection stops asserting a value and the
    effective config falls back to the catalog default of 0. This sweep runs
    once a minute over every deployment with services — so an omission here
    did not merely fail to help, it actively reset every rotated deployment's
    counter from N back to 0 and minted a revision to publish the reset,
    destroying within a minute the one signal a rotation gives a device
    (D146).

    Found by hand against a live stack, not by the suite: the device-visible
    counter went 2 -> 0 after a regeneration while the platform's own column
    read 3.
    """
    deployment_id = hierarchy["alpha"]
    with factory() as db:
        upsert_service(
            db,
            deployment_id,
            "grafana",
            config={"base_url": "https://grafana.example:3000"},
            secret_names={},
            password_secret_name=None,
        )
        db.get(Deployment, deployment_id).services_credentials_generation = 7
        db.commit()

    await service_config_sweep(factory, store, None, publish_enabled=False)

    with factory() as db:
        overrides = get_overrides(db, "deployment", str(deployment_id))
    assert overrides.get("services.credentials_generation") == 7, (
        "the sweep dropped or reset the rotation counter"
    )

    # And it is stable: a second pass must not mint a revision to change it
    # back, which is what makes the first assertion more than a coincidence.
    second = await service_config_sweep(factory, store, None, publish_enabled=False)
    assert second.revisions == 0, second


async def test_the_sweep_skips_deployments_with_no_services(factory, store) -> None:
    """A deployment nobody has configured is untouched, not written to."""
    report = await service_config_sweep(factory, store, None, publish_enabled=False)
    assert report.failures == 0


async def test_a_broker_only_deployment_never_builds_a_plan(
    factory, store, hierarchy, monkeypatch
) -> None:
    """**The common case must not cost a full merge over the fleet.**

    Every deployment with a working control plane has an `mqtt` row and, until
    the operator finishes the S5 wizard, nothing else. Its projection is empty
    and there is nothing stored to withdraw, so the sweep must return before
    `build_change_plan` — which loads every hierarchy table and recomputes
    effective config for every device under the deployment, once a minute.
    Asserted by making the planner explode if it is reached.
    """
    from app.services import config_sweep as module

    def exploding(*args, **kwargs):
        raise AssertionError(
            "build_change_plan was called for a deployment with nothing to project"
        )

    monkeypatch.setattr(module, "build_change_plan", exploding)
    report = await service_config_sweep(factory, store, None, publish_enabled=False)
    assert report.deployments >= 1, "the alpha deployment has an mqtt row and must be visited"
    assert report.failures == 0, report
    assert report.revisions == 0, report


async def test_publish_all_returns_nothing_without_a_publisher(factory) -> None:
    """The `EOE_PUBLISH_ENABLED` branch of the extracted loop (E5.7a)."""
    assert await publish_all(factory, None, [], publish_enabled=True) == set()
