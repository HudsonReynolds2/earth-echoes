"""Gate 48: the command channel (task E3.10; spec 7.2, 7.4, 13).

The phase document's acceptance is in two halves and both are here:

* `test_two_submissions_of_one_command_carry_distinct_command_ids` — two
  operator submissions are two decisions, and the ids must differ.
* `test_a_mock_device_deduplicates_a_redelivered_command` — the other side of
  the same coin, against a real Mosquitto: the SAME `command_id` arriving
  twice (QoS 1 is at-least-once) is executed once.

Those two together are the whole point of spec 7.4's `command_id`. Get the
first wrong and an operator's second "restart" is silently swallowed; get the
second wrong and one restart becomes two because the network hiccuped.
"""

import asyncio
import subprocess
import sys
import uuid
from datetime import UTC, datetime

import pytest
from conftest import (
    REPO_ROOT,
    ephemeral_broker,
    ephemeral_postgres,
    free_port,
    make_kek,
)
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from test_auth import PASSWORD

from app.auth.passwords import hash_password
from app.contracts.mqtt import Command, command_topic, decode, encode
from app.controlplane.broker import BrokerUnavailable, MqttClientManager, load_broker_coordinates
from app.db import create_session_factory
from app.devbroker import device_username, load_manifest
from app.main import API_PREFIX, create_app
from app.models import Aggregator, AuditLog, Deployment, DeploymentService, RoleAssignment, User
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy
from app.settings import Settings

RC = "redwood-coast"
AGG = "demo-agg-rc-01"
AUDIT_ACTION = "aggregator.command"

OWNER = "cmd-owner@example.com"
OTHER_OP = "cmd-other-op@example.com"
VIEWER = "cmd-viewer@example.com"

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def database():
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        yield factory


@pytest.fixture
def sessions(database):
    yield database
    with database() as db:
        db.execute(delete(AuditLog).where(AuditLog.action == AUDIT_ACTION))
        db.commit()


class FakePublisher:
    """Records what went to the broker, and can refuse like one that is down."""

    def __init__(self, fails_with: Exception | None = None) -> None:
        self.sent: list[tuple[str, bytes, bool, int]] = []
        self._fails_with = fails_with

    async def publish(
        self,
        deployment_id: uuid.UUID,
        topic: str,
        payload: bytes,
        *,
        qos: int = 1,
        retain: bool = False,
    ) -> None:
        if self._fails_with is not None:
            raise self._fails_with
        self.sent.append((topic, payload, retain, qos))


@pytest.fixture(scope="module")
def api_app(database):
    with database() as db:
        url = db.get_bind().url.render_as_string(hide_password=False)
        rc_id = db.scalars(select(Deployment.id).where(Deployment.slug == RC)).one()
        other_id = db.scalars(select(Deployment.id).where(Deployment.slug != RC)).first()
    app = create_app(
        Settings(
            database_url=url,
            session_secret="gate48-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    with app.state.session_factory() as db:
        for email, role, scope in (
            (OWNER, "owner", None),
            (OTHER_OP, "deployment_operator", other_id),
            (VIEWER, "viewer", rc_id),
        ):
            if db.scalars(select(User).where(User.email == email)).first() is not None:
                continue
            user = User(email=email, password_hash=hash_password(PASSWORD))
            user.role_assignments.append(RoleAssignment(role=role, deployment_id=scope))
            db.add(user)
        db.commit()
    return app


def login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def aggregator_id_of(factory, aggregator_uuid: str = AGG) -> uuid.UUID:
    with factory() as db:
        return db.scalars(
            select(Aggregator.id).where(Aggregator.aggregator_uuid == aggregator_uuid)
        ).one()


def send(client: TestClient, aggregator_id: uuid.UUID, command: str = "restart"):
    return client.post(
        f"{API_PREFIX}/aggregators/{aggregator_id}/commands",
        json={"command": command},
        headers={"X-CSRF-Token": client.cookies["eoe_csrf"]},
    )


def with_publisher(app, publisher):
    app.state.mqtt = publisher
    return publisher


# --- The command_id contract (spec 7.4) -------------------------------------


def test_two_submissions_of_one_command_carry_distinct_command_ids(api_app, sessions):
    """ACCEPTANCE (phase doc E3.10), first half.

    An operator pressing restart twice made two decisions. Reusing one id —
    "the same logical command" — would let the device drop the second as a
    retry, so the platform would report success for something that never
    happened.
    """
    publisher = with_publisher(api_app, FakePublisher())
    client = login(api_app, OWNER)
    agg = aggregator_id_of(sessions)
    try:
        first = send(client, agg)
        second = send(client, agg)
    finally:
        api_app.state.mqtt = None

    assert first.status_code == 202, first.text
    assert second.status_code == 202
    assert first.json()["command_id"] != second.json()["command_id"]

    on_wire = [decode(Command, payload).command_id for _, payload, _, _ in publisher.sent]
    assert len(set(on_wire)) == 2, "the bytes carried one id twice"
    assert {str(i) for i in on_wire} == {first.json()["command_id"], second.json()["command_id"]}


def test_the_command_is_published_unretained_at_qos_1(api_app, sessions):
    """Spec 7.2's table: cmd is the one platform-to-device topic that is NOT
    retained. A retained command re-fires on every reconnect — a device coming
    back after a fortnight would restart because of a button pressed then."""
    publisher = with_publisher(api_app, FakePublisher())
    client = login(api_app, OWNER)
    try:
        response = send(client, aggregator_id_of(sessions), "flush_buffer")
    finally:
        api_app.state.mqtt = None

    topic, payload, retain, qos = publisher.sent[0]
    assert retain is False, "a retained command re-fires forever"
    assert qos == 1
    assert topic == command_topic(RC, AGG) == response.json()["topic"]
    assert decode(Command, payload).command == "flush_buffer"


def test_the_topic_names_the_aggregator_uuid_not_the_platform_uuid(api_app, sessions):
    """Spec 4.2 keeps the three identifiers apart (D75). The `{agg}` segment is
    the name the device knows itself by; the platform UUID addresses nothing a
    device or an ACL has ever heard of."""
    publisher = with_publisher(api_app, FakePublisher())
    client = login(api_app, OWNER)
    agg = aggregator_id_of(sessions)
    try:
        send(client, agg)
    finally:
        api_app.state.mqtt = None

    topic = publisher.sent[0][0]
    assert AGG in topic
    assert str(agg) not in topic


@pytest.mark.parametrize("command", ["restart", "resync", "flush_buffer"])
def test_every_spec_command_is_accepted(api_app, sessions, command):
    publisher = with_publisher(api_app, FakePublisher())
    client = login(api_app, OWNER)
    try:
        response = send(client, aggregator_id_of(sessions), command)
    finally:
        api_app.state.mqtt = None
    assert response.status_code == 202
    assert decode(Command, publisher.sent[0][1]).command == command


def test_a_command_outside_the_vocabulary_is_refused_at_the_boundary(api_app, sessions):
    """`shutdown` is not in spec 7.2's list. Refusing here means no firmware
    ever has to decide what to do with a verb it does not know."""
    publisher = with_publisher(api_app, FakePublisher())
    client = login(api_app, OWNER)
    try:
        response = send(client, aggregator_id_of(sessions), "shutdown")
    finally:
        api_app.state.mqtt = None
    assert response.status_code == 422
    assert publisher.sent == []


# --- Audit ------------------------------------------------------------------


def test_the_command_is_audited_with_its_id_and_actor(api_app, sessions):
    """Spec 14.1. The id is what ties an audit row to the one attempt a device
    acted on, which is the question a post-incident conversation asks."""
    with_publisher(api_app, FakePublisher())
    client = login(api_app, OWNER)
    try:
        response = send(client, aggregator_id_of(sessions), "resync")
    finally:
        api_app.state.mqtt = None

    with sessions() as db:
        rows = list(db.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all())
    assert len(rows) == 1
    assert rows[0].detail["command_id"] == response.json()["command_id"]
    assert rows[0].detail["command"] == "resync"
    assert rows[0].actor_user_id is not None, "a command always has a human behind it"


def test_a_command_that_never_reached_the_broker_is_not_audited(api_app, sessions):
    """503 means nothing went out. An audit row would claim otherwise, and the
    timeline would show a restart that never happened."""
    with_publisher(api_app, FakePublisher(fails_with=BrokerUnavailable("no live connection")))
    client = login(api_app, OWNER)
    try:
        response = send(client, aggregator_id_of(sessions))
    finally:
        api_app.state.mqtt = None

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    with sessions() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).first() is None


# --- RBAC and refusals ------------------------------------------------------


def test_a_viewer_is_told_403_not_404(api_app, sessions):
    """They read this aggregator through GET /aggregators/{id}, so a 404 would
    be a lie about a device on their screen (the D82 rule)."""
    with_publisher(api_app, FakePublisher())
    client = login(api_app, VIEWER)
    try:
        response = send(client, aggregator_id_of(sessions))
    finally:
        api_app.state.mqtt = None
    assert response.status_code == 403
    assert "manage_devices" in response.json()["error"]["message"]


def test_an_operator_in_another_deployment_is_told_404(api_app, sessions):
    """D35: a 403 would confirm the device exists."""
    with_publisher(api_app, FakePublisher())
    client = login(api_app, OTHER_OP)
    try:
        response = send(client, aggregator_id_of(sessions))
    finally:
        api_app.state.mqtt = None
    assert response.status_code == 404


def test_commanding_requires_csrf(api_app, sessions):
    with_publisher(api_app, FakePublisher())
    client = login(api_app, OWNER)
    try:
        response = client.post(
            f"{API_PREFIX}/aggregators/{aggregator_id_of(sessions)}/commands",
            json={"command": "restart"},
        )
    finally:
        api_app.state.mqtt = None
    assert response.status_code == 403


def test_an_unknown_aggregator_is_a_404(api_app, sessions):
    with_publisher(api_app, FakePublisher())
    client = login(api_app, OWNER)
    try:
        response = send(client, uuid.uuid4())
    finally:
        api_app.state.mqtt = None
    assert response.status_code == 404


def test_commanding_refuses_while_publication_is_disabled(api_app, sessions):
    """The API holds no outbound connection with the flag off (D86), and
    commands ride the same socket as publishes."""
    api_app.state.mqtt = None
    client = login(api_app, OWNER)
    response = send(client, aggregator_id_of(sessions))
    assert response.status_code == 409
    assert "EOE_PUBLISH_ENABLED" in response.json()["error"]["message"]


# --- ACCEPTANCE: a real device deduplicating a real redelivery --------------


def _provision(url: str, kek: str, out_dir) -> None:
    import os

    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--out", str(out_dir)],
        cwd=REPO_ROOT / "backend",
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": url, "EOE_KEK": kek, "EOE_SESSION_SECRET": "gate48"},
        timeout=180,
    )
    assert result.returncode == 0, f"devbroker failed: {result.stdout}\n{result.stderr}"


@pytest.fixture
def live_stack(tmp_path_factory):
    out = tmp_path_factory.mktemp("cmd-certs")
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


class MockDevice:
    """An Aggregator that runs each command exactly once.

    The dedup is spec 7.4's, done the way firmware would: remember the
    `command_id`s already acted on and drop a repeat. Deliberately NOT keyed
    on the command name — that would make an operator's second deliberate
    restart a no-op, which is the failure the other half of this acceptance
    guards against.
    """

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.seen: set[uuid.UUID] = set()

    def deliver(self, payload: bytes) -> None:
        command = decode(Command, payload)
        if command.command_id in self.seen:
            return
        self.seen.add(command.command_id)
        self.executed.append(command.command)


async def test_a_mock_device_deduplicates_a_redelivered_command(live_stack):
    """ACCEPTANCE (phase doc E3.10), second half, against a real broker.

    One command goes out over a real TLS connection and is read by a
    subscriber authenticated as the device itself, so the spec 7.1 ACL is on
    trial too. The device is then handed the SAME bytes again — which is what
    QoS 1 at-least-once means in practice — and runs it once. A genuinely new
    submission, carrying its own id, runs.
    """
    broker, factory, store, manifest = live_stack
    with factory() as db:
        deployment_id = db.scalars(select(Deployment.id).where(Deployment.slug == RC)).one()
    username = device_username(AGG)
    password = next(a["password"] for a in manifest["accounts"] if a["username"] == username)

    # The cmd topic is NOT retained, so the device has to be listening before
    # the command goes out — exactly the real-world property that makes a
    # command a one-shot and an offline device miss it.
    subscriber = asyncio.create_task(
        asyncio.to_thread(
            broker.exec_client,
            "mosquitto_sub",
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
            command_topic(RC, AGG),
            "-C",
            "1",
            "-W",
            "20",
        )
    )
    await asyncio.sleep(1.0)

    first = Command(at=datetime.now(UTC), command="restart")
    manager = MqttClientManager(lambda: load_broker_coordinates(factory, store))
    async with manager:
        await manager.wait_connected(deployment_id)
        await manager.publish(deployment_id, command_topic(RC, AGG), encode(first), retain=False)
        result = await subscriber

    assert result.returncode == 0, result.stderr
    on_wire = result.stdout.strip().encode()
    assert decode(Command, on_wire).command_id == first.command_id

    device = MockDevice()
    device.deliver(on_wire)
    device.deliver(on_wire)
    assert device.executed == ["restart"], "QoS 1 redelivery ran the command twice"

    second = Command(at=datetime.now(UTC), command="restart")
    assert second.command_id != first.command_id
    device.deliver(encode(second))
    assert device.executed == ["restart", "restart"], (
        "the device deduplicated a NEW command, so an operator's second "
        "deliberate restart was silently swallowed"
    )
