"""Gate 40: the MQTT client manager (task E3.2; spec 7.1, 7.4).

The phase document's acceptance criterion is one test:
`test_a_broker_restart_is_invisible_to_message_handling` at the bottom. It
kills a real broker under a connected manager, restarts it, and asserts that
the handler registered before any of that happened receives a message
published afterwards — which can only be true if the manager reconnected AND
replayed its subscriptions, without the handler being told anything.

Everything above it is cheaper machinery: the backoff arithmetic, the TLS
anchor choice, credential hygiene, and the database loader. They exist so that
a red acceptance test means the connection loop is wrong, rather than the
password or the certificate.
"""

import asyncio
import contextlib
import os
import ssl
import subprocess
import sys
import uuid

import aiomqtt
import pytest
from conftest import (
    REPO_ROOT,
    ephemeral_broker,
    ephemeral_postgres,
    free_port,
    make_kek,
)
from sqlalchemy import select, update

from app.contracts.mqtt import aggregator_root, deployment_root, deployment_subscriptions
from app.controlplane.broker import (
    Backoff,
    BrokerCoordinates,
    BrokerUnavailable,
    InboundMessage,
    MqttClientManager,
    _open_client,
    load_broker_coordinates,
    tls_context,
)
from app.db import create_session_factory
from app.devbroker import device_username, load_manifest, platform_username, secret_name
from app.models import DeploymentService
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy

BACKEND = REPO_ROOT / "backend"

RC = "redwood-coast"
HD = "high-desert"
AGG_A = "demo-agg-rc-01"

pytestmark = pytest.mark.anyio


def coordinates(**overrides: object) -> BrokerCoordinates:
    values: dict[str, object] = {
        "deployment_id": uuid.uuid4(),
        "slug": RC,
        "host": "127.0.0.1",
        "port": 8883,
        "username": platform_username(RC),
        "password": "the-platform-password",
    }
    values.update(overrides)
    return BrokerCoordinates(**values)  # type: ignore[arg-type]


# --- Backoff: pure arithmetic, no sleeping ---------------------------------


def test_backoff_grows_exponentially_and_stops_at_the_ceiling():
    backoff = Backoff(initial=1.0, maximum=30.0, factor=2.0, jitter=0.0)
    unjittered = [backoff.delay(attempt) for attempt in range(1, 8)]
    assert unjittered == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_backoff_jitter_stays_inside_its_band_and_never_exceeds_the_ceiling():
    backoff = Backoff(initial=4.0, maximum=10.0, factor=2.0, jitter=0.25)
    assert backoff.delay(1, rng=lambda: 0.0) == pytest.approx(3.0)
    assert backoff.delay(1, rng=lambda: 1.0) == pytest.approx(5.0)
    # The ceiling binds AFTER jitter, so a jittered delay can never overshoot
    # it — an unbounded retry gap is how a deployment goes quietly deaf.
    assert backoff.delay(5, rng=lambda: 1.0) == pytest.approx(10.0)


def test_backoff_rejects_attempt_zero():
    """Attempts are 1-based; an off-by-one would silently halve every delay."""
    with pytest.raises(ValueError):
        Backoff().delay(0)


# --- TLS: the stored CA is the anchor, and the only one --------------------


def test_a_stored_ca_is_the_only_trust_anchor():
    """Spec 7.1 identifies a deployment's broker by its own CA. Adding the
    public trust store beside it would mean any public CA's certificate for
    the broker's hostname verified too, which is weaker than the check the
    stored PEM exists to make."""
    from app.devbroker import generate_tls_material

    material = generate_tls_material()
    context = tls_context(coordinates(ca_cert_pem=material["ca.crt"].decode()))
    assert context is not None
    anchors = context.get_ca_certs()
    assert len(anchors) == 1, "the pinned CA is not the only anchor"
    assert dict(anchors[0]["subject"][0])["commonName"] == "Echoes of Earth dev CA"  # type: ignore[index]
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_without_a_stored_ca_the_public_trust_store_is_used():
    """E5 will onboard brokers with publicly-issued certificates; that path
    must still verify, not fall through to no verification at all."""
    context = tls_context(coordinates(ca_cert_pem=None))
    assert context is not None
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_tls_can_be_switched_off_only_by_the_row():
    assert tls_context(coordinates(tls_enabled=False)) is None


# --- Credential hygiene (rule R2) ------------------------------------------


def test_coordinates_never_render_their_password():
    coords = coordinates(password="s3cr3t-broker-password")
    for rendered in (repr(coords), str(coords), f"connected to the {coords}"):
        assert "s3cr3t-broker-password" not in rendered
    assert coords.slug in str(coords)


# --- Registration rules -----------------------------------------------------


async def test_subscriptions_are_fixed_once_the_manager_is_running():
    """A late registration would apply to a connection that is currently down
    only after it reconnected, so some deployments would deliver to it and
    others would not."""
    manager = MqttClientManager(lambda: [])
    await manager.start()
    try:
        with pytest.raises(RuntimeError, match="before start"):
            manager.subscribe(deployment_subscriptions, _collect([]))
    finally:
        await manager.stop()


async def test_starting_twice_is_refused():
    manager = MqttClientManager(lambda: [])
    async with manager:
        with pytest.raises(RuntimeError, match="already started"):
            await manager.start()


async def test_stop_is_idempotent_and_safe_before_start():
    manager = MqttClientManager(lambda: [])
    await manager.stop()
    await manager.stop()
    assert manager.deployment_ids == ()


async def test_publishing_without_a_connection_is_a_named_failure():
    """E3.4 must be able to tell 'the broker is down' from 'published': a
    revision that moved to pending because a silent publish returned would be
    a lie the timeline then repeats."""
    manager = MqttClientManager(lambda: [])
    async with manager:
        with pytest.raises(BrokerUnavailable):
            await manager.publish(uuid.uuid4(), "eoe/x/agg/y/desired", b"{}")


async def test_wait_connected_rejects_an_unmanaged_deployment():
    manager = MqttClientManager(lambda: [])
    async with manager:
        with pytest.raises(BrokerUnavailable):
            await manager.wait_connected(uuid.uuid4(), timeout=0.1)


# --- The database loader ----------------------------------------------------


def _provision(url: str, kek: str, out, host: str = "127.0.0.1") -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--out", str(out), "--host", host],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": url,
            "EOE_SESSION_SECRET": f"mqtt-manager-{uuid.uuid4().hex}",
            "EOE_KEK": kek,
        },
        timeout=180,
    )
    assert result.returncode == 0, f"devbroker failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.integration
def test_the_loader_reads_the_mqtt_row_and_resolves_the_password(tmp_path):
    """The connection details come from `deployment_service`, and the password
    comes from SecretStore — never from the row (rule R2)."""
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        _provision(url, kek, tmp_path)
        store = SecretStore(factory, kek)

        loaded = load_broker_coordinates(factory, store)
        assert [c.slug for c in loaded] == [HD, RC], "deterministic order, by slug"
        manifest = load_manifest(tmp_path)
        expected = {a["username"]: a["password"] for a in manifest["accounts"]}
        for coords in loaded:
            assert coords.username == platform_username(coords.slug)
            assert coords.password == expected[coords.username]
            assert coords.tls_enabled is True
            assert coords.ca_cert_pem is not None
            assert coords.port == 8883


@pytest.mark.integration
def test_one_unreadable_secret_does_not_cost_every_other_deployment_its_broker(tmp_path):
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        _provision(url, kek, tmp_path)
        store = SecretStore(factory, kek)

        with factory() as db:
            broken = db.scalars(select(DeploymentService)).first()
            assert broken is not None
            broken_id = broken.deployment_id
        store.delete(secret_name(broken_id))

        loaded = load_broker_coordinates(factory, store)
        assert len(loaded) == 1, "the healthy deployment was dropped along with the broken one"
        assert loaded[0].deployment_id != broken_id


# --- Live-broker behaviour --------------------------------------------------


def _collect(sink: list[InboundMessage]):
    async def handler(message: InboundMessage) -> None:
        sink.append(message)

    return handler


async def _until(predicate, what: str, timeout: float = 20.0) -> None:
    """Poll a condition on the event loop instead of sleeping a fixed amount:
    a fixed sleep is either flaky on a loaded gate machine or slow on a fast
    one, and the gate has to be neither."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.05)


@pytest.fixture(scope="module")
def live_broker(tmp_path_factory):
    """A provisioned broker on a STABLE host port, with its `deployment_service`
    rows pointing at that port, so the manager dials the same socket before and
    after the restart in the acceptance test."""
    out = tmp_path_factory.mktemp("mgr-certs")
    port = free_port()
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        _provision(url, kek, out)
        with factory() as db:
            db.execute(update(DeploymentService).values(port=port))
            db.commit()
        with ephemeral_broker(out, host_port=port) as broker:
            yield broker, factory, SecretStore(factory, kek), load_manifest(out)


def _publish_from_a_device(broker, manifest, topic: str, payload: str, retain: bool = False):
    """Publish as the aggregator's own dev account, inside the container. The
    point is that the message reaches the manager over the wire from a real
    third party, not from the code under test."""
    username = device_username(AGG_A)
    password = next(a["password"] for a in manifest["accounts"] if a["username"] == username)
    result = broker.exec_client(
        "mosquitto_pub",
        "-h",
        "localhost",
        "-p",
        "8883",
        "--cafile",
        "/mosquitto/dev/ca.crt",
        "-u",
        username,
        "-P",
        password,
        "-t",
        topic,
        "-m",
        payload,
        "-q",
        "1",
        *(["-r"] if retain else []),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def _rc_deployment_id(coords_list) -> uuid.UUID:
    return next(c.deployment_id for c in coords_list if c.slug == RC)


@pytest.mark.integration
async def test_the_manager_connects_over_tls_and_delivers_to_its_handler(live_broker):
    broker, factory, store, manifest = live_broker
    received: list[InboundMessage] = []
    coords = load_broker_coordinates(factory, store)
    manager = MqttClientManager(lambda: coords)
    manager.subscribe(deployment_subscriptions, _collect(received))

    async with manager:
        deployment_id = _rc_deployment_id(coords)
        await manager.wait_connected(deployment_id)
        topic = f"{aggregator_root(RC, AGG_A)}/reported"
        _publish_from_a_device(broker, manifest, topic, '{"hello":"platform"}')
        await _until(lambda: any(m.topic == topic for m in received), "the reported message")

    message = next(m for m in received if m.topic == topic)
    assert message.payload == b'{"hello":"platform"}'
    assert message.deployment_id == deployment_id
    assert message.deployment_slug == RC, "handlers get the identity the topic alone cannot give"
    assert message.qos == 1


@pytest.mark.integration
async def test_the_manager_subscribes_to_device_topics_only(live_broker):
    """`deployment_subscriptions` excludes desired and cmd; if the manager
    widened that to `eoe/{dep}/#` it would feed the platform's own retained
    publishes back into its consumer, and every publish would look like a
    device report."""
    broker, factory, store, manifest = live_broker
    received: list[InboundMessage] = []
    coords = load_broker_coordinates(factory, store)
    manager = MqttClientManager(lambda: coords)
    manager.subscribe(deployment_subscriptions, _collect(received))

    async with manager:
        deployment_id = _rc_deployment_id(coords)
        await manager.wait_connected(deployment_id)
        desired = f"{aggregator_root(RC, AGG_A)}/desired"
        event = f"{aggregator_root(RC, AGG_A)}/event"
        await manager.publish(deployment_id, desired, b'{"desired":true}', retain=True)
        _publish_from_a_device(broker, manifest, event, '{"code":"boot"}')
        await _until(lambda: any(m.topic == event for m in received), "the event message")

    assert not any(m.topic == desired for m in received), "the platform echoed its own publish back"


@pytest.mark.integration
async def test_publish_reaches_the_broker_retained(live_broker):
    """E3.4 builds the desired-config publish on this; retained delivery to a
    subscriber that connects AFTERWARDS is the spec 6.4 reconnect property."""
    broker, factory, store, manifest = live_broker
    coords = load_broker_coordinates(factory, store)
    manager = MqttClientManager(lambda: coords)
    marker = f"retained-{uuid.uuid4().hex[:8]}"
    topic = f"{aggregator_root(RC, 'demo-agg-rc-03')}/desired"

    async with manager:
        deployment_id = _rc_deployment_id(coords)
        await manager.wait_connected(deployment_id)
        await manager.publish(deployment_id, topic, marker.encode(), retain=True)

    username = platform_username(RC)
    password = next(a["password"] for a in manifest["accounts"] if a["username"] == username)
    subscribed = broker.exec_client(
        "mosquitto_sub",
        *[
            "-h",
            "localhost",
            "-p",
            "8883",
            "--cafile",
            "/mosquitto/dev/ca.crt",
            "-u",
            username,
            "-P",
            password,
            "-t",
            topic,
            "-W",
            "5",
            "-C",
            "1",
        ],
        timeout=60,
    )
    assert subscribed.returncode == 0, subscribed.stderr
    assert subscribed.stdout.strip() == marker


@pytest.mark.integration
async def test_a_raising_handler_does_not_break_the_connection(live_broker):
    """One device's malformed payload must not cost a whole deployment its
    control plane: the manager logs the handler failure and keeps reading."""
    broker, factory, store, manifest = live_broker
    seen: list[str] = []

    async def explodes(message: InboundMessage) -> None:
        seen.append(message.topic)
        if len(seen) == 1:
            raise ValueError("the first message is poison")

    coords = load_broker_coordinates(factory, store)
    manager = MqttClientManager(lambda: coords)
    manager.subscribe(deployment_subscriptions, explodes)

    async with manager:
        deployment_id = _rc_deployment_id(coords)
        await manager.wait_connected(deployment_id)
        topic = f"{aggregator_root(RC, AGG_A)}/event"
        _publish_from_a_device(broker, manifest, topic, "poison")
        await _until(lambda: len(seen) == 1, "the poison message")
        _publish_from_a_device(broker, manifest, topic, "fine")
        await _until(lambda: len(seen) == 2, "the message after the poison one")
        assert manager.is_connected(deployment_id), "a handler exception dropped the connection"


@pytest.mark.integration
async def test_a_broker_restart_is_invisible_to_message_handling(live_broker):
    """THE E3.2 ACCEPTANCE CRITERION, stated in the phase document: kill and
    restart the dev broker under test; the manager reconnects and resubscribes
    without message-handling code noticing.

    'Without noticing' is asserted literally. The handler is registered once,
    before the outage, and is never told the connection dropped; the only
    thing it observes is that a message published after the restart arrives.
    That message can only reach it if the manager both reconnected AND
    replayed its subscription set, since Mosquitto does not remember the
    subscriptions of a clean session.
    """
    broker, factory, store, manifest = live_broker
    received: list[InboundMessage] = []
    coords = load_broker_coordinates(factory, store)
    # A tight backoff so the gate does not wait out a production-shaped one;
    # the ceiling still proves the cap is applied on the reconnect path.
    manager = MqttClientManager(lambda: coords, backoff=Backoff(initial=0.2, maximum=1.0))
    manager.subscribe(deployment_subscriptions, _collect(received))
    topic = f"{aggregator_root(RC, AGG_A)}/reported"

    async with manager:
        deployment_id = _rc_deployment_id(coords)
        await manager.wait_connected(deployment_id)
        _publish_from_a_device(broker, manifest, topic, "before")
        await _until(lambda: len(received) == 1, "the pre-outage message")

        broker.stop()
        await _until(lambda: not manager.is_connected(deployment_id), "the connection to drop")

        broker.start()
        await _until(lambda: manager.is_connected(deployment_id), "the reconnect", timeout=60.0)

        _publish_from_a_device(broker, manifest, topic, "after")
        await _until(lambda: len(received) == 2, "the post-restart message")

    payloads = [m.payload for m in received]
    assert payloads == [b"before", b"after"]
    assert {m.topic for m in received} == {topic}


@pytest.mark.integration
async def test_a_broker_that_never_answers_is_retried_rather_than_abandoned(live_broker):
    """An unreachable broker is a retry, not a crash — and not a task that
    quietly exits, which would leave that deployment deaf until a restart."""
    _, factory, store, _ = live_broker
    coords = load_broker_coordinates(factory, store)
    unreachable = [
        BrokerCoordinates(
            deployment_id=uuid.uuid4(),
            slug="nowhere",
            host="127.0.0.1",
            port=free_port(),  # nothing is listening there
            username="nobody",
            password="nothing",
            tls_enabled=True,
            ca_cert_pem=coords[0].ca_cert_pem,
        )
    ]
    manager = MqttClientManager(lambda: unreachable, backoff=Backoff(initial=0.1, maximum=0.2))

    async with manager:
        deployment_id = unreachable[0].deployment_id
        with pytest.raises(TimeoutError):
            await manager.wait_connected(deployment_id, timeout=2.0)
        assert deployment_id in manager.deployment_ids, "the connection task gave up and vanished"
        with pytest.raises(BrokerUnavailable):
            await manager.publish(deployment_id, f"{deployment_root(RC)}/x", b"{}")


async def _tasks_outliving(before: set[asyncio.Task], settle: float = 10.0) -> set[asyncio.Task]:
    """Tasks still alive after `stop()`, allowing cancellation already in
    flight to land (D97, widened by D121, made decisive by D123).

    **Nothing is excused, and nothing waits longer than it has to.** The two
    goals are the same goal: classify the survivor instead of timing it out.

    * A survivor still holding a LIVE socket is returned IMMEDIATELY. It is the
      leak D94 was written for — a connection the process keeps servicing — and
      `loop_misc()` will return success under it forever, so every second spent
      waiting reaches the identical verdict later. The old code sat out its
      whole deadline to do this, which is why a failure used to cost 30s.
    * A survivor with no socket is given `settle` to disappear, because
      `loop_misc()` returns `MQTT_ERR_NO_CONN` as soon as `_sock is None` and
      the loop ends within one of its own 1-second iterations. If it is STILL
      here at the deadline, that is a contradiction of the mechanism and it
      fails — the wait proves the exit, it does not excuse the absence of one.
    * A survivor whose shape we cannot read is not assumed to be either. It
      waits with the rest, and fails at the deadline if it is still there.

    Any wait at all is needed because aiomqtt cancels its own `_misc_loop` with
    `call_soon_threadsafe(self._misc_task.cancel)` — from `_on_socket_close`,
    NOT from `__aexit__`, which never touches that task (aiomqtt 2.5.1
    client.py; an earlier version of this docstring said `__aexit__` and was
    wrong). paho calls that back on its own thread, so the cancel is SCHEDULED
    on the event loop and nothing awaits it. No amount of correctness in
    `stop()` can make a third party's `call_soon_threadsafe` synchronous, short
    of reaching into private attributes of the library.

    **The immediate-fail rule below is load-bearing and was right.** It fired
    once under a loaded gate on a survivor that was still CONNECTED, and that
    turned out to be a real leak at the OTHER end of the lifecycle — a
    cancellation inside `__aenter__` stranding a connected client off the stack
    (D150, `_open_client`). A live socket here is not a teardown in flight: a
    teardown that has reached `__aexit__` has already resolved `_disconnected`.

    Nothing here excuses a task by NAME. D94's leak was an anonymous
    `Client._misc_loop`, not the manager's own `mqtt-{slug}` connection task, so
    filtering to platform-named tasks would delete the detector that found a
    real per-reconnect leak of tasks and sockets in a process meant to run for
    months. The discriminator is the socket, which is the thing the leak
    actually costs.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settle
    while True:
        leaked = {t for t in asyncio.all_tasks() if not t.done()} - before
        if not leaked:
            return set()
        # A survivor still holding a socket is the real leak, and no amount of
        # waiting will retire it: `loop_misc()` goes on returning success for
        # as long as there is a connection under it. Return NOW rather than
        # sitting out the deadline to reach the same answer more slowly.
        holding = {task for task in leaked if _paho_socket_is_live(task)}
        if holding:
            return holding
        if loop.time() > deadline:
            return leaked
        await asyncio.sleep(0.05)


def _paho_client(task: asyncio.Task):
    """The aiomqtt client a task belongs to, or None if it is not one.

    Read out of the coroutine's own frame because aiomqtt exposes no accessor.
    """
    frame = task.get_coro().cr_frame  # type: ignore[union-attr]
    return getattr(frame.f_locals.get("self"), "_client", None) if frame else None


def _paho_socket_is_live(task: asyncio.Task) -> bool:
    """Whether this survivor is provably holding a broker connection.

    Only used to decide whether to fail NOW or to keep waiting — never to
    excuse anything. A task this cannot read is therefore False rather than
    True: "unreadable" is not evidence of a leak, and an unrelated task passing
    through the loop for a moment would otherwise fail the test on shape alone,
    where simply watching it disappear is both stricter and fairer. Whatever it
    is, if it is still there at the deadline it fails anyway.
    """
    with contextlib.suppress(Exception):
        paho = _paho_client(task)
        return paho is not None and paho.socket() is not None
    return False


def describe_leaked_task(task: asyncio.Task) -> str:
    """Name a survivor AND say whether it still holds a socket.

    The distinction decides what a failure here means, and the bare task name
    cannot express it. `_misc_loop` ends itself once paho's `_sock` is None, so
    a survivor with no socket is a teardown that has not finished landing —
    while a survivor WITH a live socket is the real leak D94 was written for: a
    connection the process will hold, and keep servicing, forever.

    Read out of the coroutine's own frame because aiomqtt exposes no accessor,
    and defensively: this runs only on the failure path, and a diagnostic that
    raises would replace the real assertion message with its own traceback.
    """
    name = task.get_name()
    try:
        paho = _paho_client(task)
        if paho is None:
            return f"{name} (not an aiomqtt client task, and therefore unexplained)"
        socket_state = "LIVE" if paho.socket() is not None else "gone"
        return f"{name} socket={socket_state} state={paho._state!r}"
    except Exception as exc:  # pragma: no cover - diagnostics must never mask
        return f"{name} (could not inspect: {exc!r})"


@pytest.mark.integration
async def test_shutdown_leaves_no_running_tasks(live_broker):
    """Clean shutdown, asserted rather than assumed: a leaked connection task
    would keep a broker session (and a Postgres-less asyncio loop) alive past
    the test that made it."""
    _, factory, store, _ = live_broker
    coords = load_broker_coordinates(factory, store)
    manager = MqttClientManager(lambda: coords)
    before = {t for t in asyncio.all_tasks() if not t.done()}

    await manager.start()
    await manager.wait_connected(_rc_deployment_id(coords))
    await manager.stop()

    leaked = await _tasks_outliving(before)
    assert not leaked, f"tasks outlived stop(): {[describe_leaked_task(t) for t in leaked]}"
    assert manager.deployment_ids == ()
    assert not manager.is_connected(_rc_deployment_id(coords))


@pytest.mark.integration
async def test_a_cancelled_connect_cannot_strand_a_connected_client(live_broker):
    """D150, forced rather than waited for.

    aiomqtt's `__aenter__` awaits twice — paho's blocking `connect()` in an
    executor thread, then the CONNACK. A cancellation at either point used to
    abandon `enter_async_context` BEFORE it registered the client, while the
    executor thread ran the connect through to completion anyway (a started
    thread cannot be cancelled). What survived was a CONNECTED client with a
    live socket and a running `_misc_loop` that no `AsyncExitStack` owned, so
    `stop()` could not close it and nothing ever would.

    This is what `test_shutdown_leaves_no_running_tasks` above caught under a
    loaded gate, once, as `socket=LIVE state=MQTT_CS_CONNECTED`. It reproduces
    it deterministically instead: the socket is proven OPEN before the cancel,
    so a pass cannot be the vacuous one where nothing had connected yet.
    """
    _, factory, store, _ = live_broker
    coords = next(c for c in load_broker_coordinates(factory, store) if c.slug == RC)
    client = aiomqtt.Client(
        hostname=coords.host,
        port=coords.port,
        username=coords.username,
        password=coords.password,
        identifier=f"eoe-strand-{uuid.uuid4().hex[:8]}",
        tls_context=tls_context(coords),
        keepalive=30,
        clean_session=True,
    )
    stack = contextlib.AsyncExitStack()
    entering = asyncio.create_task(_open_client(stack, client))

    # The leak lives in the window between paho opening the socket and
    # `__aenter__` returning, so wait until the socket provably exists.
    for _ in range(500):
        if client._client.socket() is not None:
            break
        await asyncio.sleep(0.01)
    assert client._client.socket() is not None, (
        "never connected, so cancelling here would prove nothing"
    )

    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering

    # The cancellation must not have cost the stack its ownership: closing it
    # has to take the connection down. Before the fix this closed nothing and
    # the socket below stayed live.
    await stack.aclose()
    assert client._client.socket() is None, "a cancelled connect stranded a live socket"


async def test_start_or_retry_survives_an_unreadable_coordinates_query():
    """`start()` reads the `deployment_service` rows once, so what fails here
    is the DATABASE — in compose, the manager's host can win the race against
    the migrations that create that table (D87). Neither host may die of it.
    """
    attempts = []

    def loader():
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise RuntimeError('relation "deployment_service" does not exist')
        return []

    manager = MqttClientManager(loader)
    before = {t for t in asyncio.all_tasks() if not t.done()}

    assert await manager.start_or_retry() is False, "a failed first attempt is not a live start"
    # The retry backs off from 2s; it must reach a healthy load on its own.
    for _ in range(100):
        if len(attempts) >= 3:
            break
        await asyncio.sleep(0.1)
    await manager.stop()

    assert len(attempts) >= 3, f"the background retry stopped trying after {len(attempts)}"
    leaked = {t for t in asyncio.all_tasks() if not t.done()} - before
    assert not leaked, f"tasks outlived stop(): {[t.get_name() for t in leaked]}"


async def test_start_or_retry_is_a_plain_start_when_the_query_works():
    manager = MqttClientManager(lambda: [])
    assert await manager.start_or_retry() is True
    await manager.stop()


async def test_stopping_cancels_a_retry_that_is_still_trying():
    """A retry left running past `stop()` would hold a session factory open and
    reconnect a manager its owner already shut down."""
    manager = MqttClientManager(lambda: (_ for _ in ()).throw(RuntimeError("still down")))
    before = {t for t in asyncio.all_tasks() if not t.done()}

    assert await manager.start_or_retry() is False
    await manager.stop()

    leaked = {t for t in asyncio.all_tasks() if not t.done()} - before
    assert not leaked, f"a retry outlived stop(): {[t.get_name() for t in leaked]}"
