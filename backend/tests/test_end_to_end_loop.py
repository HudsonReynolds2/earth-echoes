"""Gate 51: the whole loop, UI request to device and back (task E3.13).

Epic E3's definition of done, as one test against a real Mosquitto and a real
worker:

    edit config through the API -> preview -> apply -> the desired config
    reaches the device's retained topic -> the device acks -> the revision
    reaches `applied` -> the timeline and the websocket say so.

Everything below it in this file guards the seams E3.13 itself changed: apply
now publishes, `EOE_PUBLISH_ENABLED` defaults ON (D61), and a broker that is
down must cost a publish rather than an operator's edit.

This is deliberately the ONE test that spans every task in the epic. The
per-task suites are where a failure is diagnosable; this one is where "it
actually works" is asserted, and a red here with green everywhere else means
the pieces do not fit rather than that a piece is broken.
"""

import asyncio
import os
import subprocess
import sys
import uuid

import pytest
from conftest import (
    REPO_ROOT,
    ephemeral_broker,
    ephemeral_postgres,
    free_port,
    make_kek,
)
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_auth import PASSWORD

from app.auth.passwords import hash_password
from app.contracts.mqtt import (
    DesiredConfig,
    ReportedAggregatorState,
    decode,
    desired_topic,
    encode,
    reported_topic,
)
from app.controlplane.broker import load_broker_coordinates
from app.controlplane.revision_state import RevisionState
from app.controlplane.runner import ReconciliationWorker
from app.db import create_session_factory
from app.devbroker import device_username, load_manifest
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    ConfigRevision,
    DeploymentService,
    ReconciliationEvent,
    RoleAssignment,
    User,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy
from app.settings import Settings

RC = "redwood-coast"
AGG = "demo-agg-rc-01"
OWNER = "e2e-owner@example.com"

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _provision(url: str, kek: str, out_dir) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--out", str(out_dir)],
        cwd=REPO_ROOT / "backend",
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": url, "EOE_KEK": kek, "EOE_SESSION_SECRET": "gate51"},
        timeout=180,
    )
    assert result.returncode == 0, f"devbroker failed: {result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def platform(tmp_path_factory):
    """A whole platform: migrated database, provisioned broker, and an API
    whose lifespan has actually run — so `app.state.mqtt` is the real outbound
    manager and `app.state.hub` the real websocket fan-out."""
    out = tmp_path_factory.mktemp("e2e-certs")
    port = free_port()
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        _provision(url, kek, out)
        with factory() as db:
            # The broker row says `mosquitto:8883` for the compose network; the
            # test reaches the same broker on a host port.
            db.execute(update(DeploymentService).values(host="localhost", port=port))
            db.commit()
        with ephemeral_broker(out, host_port=port) as broker:
            app = create_app(
                Settings(
                    database_url=url,
                    session_secret="gate51-secret",
                    # The SAME kek the broker secrets were wrapped with. A
                    # fresh one reads as "every broker row is unreadable",
                    # which the manager survives by design and which would
                    # make this test quietly prove nothing.
                    kek=kek,
                    cors_origins="",
                )
            )
            with app.state.session_factory() as db:
                user = User(email=OWNER, password_hash=hash_password(PASSWORD))
                user.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
                db.add(user)
                db.commit()
            yield app, broker, factory, SecretStore(factory, kek), load_manifest(out)


def login(app) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def device_credentials(manifest) -> tuple[str, str]:
    username = device_username(AGG)
    return username, next(a["password"] for a in manifest["accounts"] if a["username"] == username)


def platform_uuid_of(factory) -> str:
    with factory() as db:
        return str(db.scalars(select(Aggregator.id).where(Aggregator.aggregator_uuid == AGG)).one())


def read_desired(broker, manifest) -> DesiredConfig:
    """Read the retained desired message as the device itself, so the spec 7.1
    ACL is on trial too."""
    username, password = device_credentials(manifest)
    result = broker.exec_client(
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
        desired_topic(RC, AGG),
        "-C",
        "1",
        "-W",
        "15",
    )
    assert result.returncode == 0, result.stderr
    return decode(DesiredConfig, result.stdout.strip())


def state_of(factory, revision_id: uuid.UUID) -> str:
    with factory() as db:
        return db.scalars(
            select(ConfigRevision.state).where(ConfigRevision.id == revision_id)
        ).one()


async def wait_for_state(factory, revision_id: uuid.UUID, wanted: RevisionState) -> None:
    for _ in range(150):
        if state_of(factory, revision_id) == wanted.value:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"revision stayed {state_of(factory, revision_id)}, wanted {wanted}")


async def test_a_config_edit_reaches_a_device_and_comes_back_applied(platform):
    """THE definition of done for epic E3 (phase doc §5, task E3.13).

    Nothing is nudged along by hand: the API publishes because apply asked it
    to, the device answers on its own credential, and the worker's consumer
    moves the revision because the report matched.
    """
    app, broker, factory, store, manifest = platform
    with TestClient(app) as _lifespan:  # starts the outbound manager and the hub
        client = login(app)
        agg_id = platform_uuid_of(factory)

        worker = ReconciliationWorker(factory, store, timeout_interval=3600, drift_interval=3600)
        async with worker:
            await worker.manager.wait_connected(
                next(
                    c.deployment_id for c in load_broker_coordinates(factory, store) if c.slug == RC
                )
            )

            # 1. Preview, exactly as the UI does.
            body = {
                "selection": {"entity_type": "aggregator", "where": {"ids": [agg_id]}},
                "changes": {"logging.verbosity": "debug"},
                "level": "target",
            }
            preview = client.post(f"{API_PREFIX}/config/preview", json=body)
            assert preview.status_code == 200, preview.text
            assert preview.json()["items"][0]["changed_keys"] == ["logging.verbosity"]

            # 2. Apply — which now publishes, because E3.13 wired it through.
            applied = await asyncio.to_thread(
                client.post,
                f"{API_PREFIX}/config/apply",
                json=body,
                headers={"X-CSRF-Token": client.cookies["eoe_csrf"]},
            )
            assert applied.status_code == 200, applied.text
            result = applied.json()
            assert result["publish_enabled"] is True
            # An aggregator-level change moves its Listeners' effective config
            # too, so E2 cuts a revision per affected device. All go out.
            assert result["published"] >= 1, "apply did not publish (E3.13's whole point)"
            assert result["published"] == len(result["revisions"])
            assert result["state"] == "pending"
            assert all(r["state"] == "pending" for r in result["revisions"])
            aggregator_revision = next(
                r for r in result["revisions"] if r["target_type"] == "aggregator"
            )
            revision_id = uuid.UUID(aggregator_revision["revision_id"])

            # 3. The desired config is on the device's retained topic.
            desired = await asyncio.to_thread(read_desired, broker, manifest)
            assert desired.revision_id == revision_id
            assert desired.config["logging.verbosity"] == "debug"
            assert desired.target.id == AGG, "the topic names the aggregator_uuid (D75)"

            # 4. The device applies it and says so, on its own credential.
            username, password = device_credentials(manifest)
            report = ReportedAggregatorState(
                reported_at=desired.generated_at,
                applied_revision_id=revision_id,
                config=dict(desired.config),
                checksum=desired.checksum,
            )
            published = await asyncio.to_thread(
                broker.exec_client,
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
                "-q",
                "1",
                "-t",
                reported_topic(RC, AGG),
                "-m",
                encode(report).decode(),
            )
            assert published.returncode == 0, published.stderr

            # 5. The worker's consumer moves it to applied.
            await wait_for_state(factory, revision_id, RevisionState.APPLIED)

    # 6. The timeline tells the story, and the API agrees.
    timeline = client.get(f"{API_PREFIX}/aggregators/{agg_id}/timeline").json()["items"]
    assert [(e["from_state"], e["to_state"]) for e in timeline] == [
        ("pending", "applied"),
        ("draft", "pending"),
    ]
    assert timeline[1]["actor_email"] == OWNER, "the publish was the operator's"
    assert timeline[0]["actor_email"] is None, "the ack was the device's"

    listing = client.get(f"{API_PREFIX}/aggregators").json()["items"]
    assert next(item for item in listing if item["id"] == agg_id)["status"] in {
        "healthy",
        "unknown",
    }


async def test_the_websocket_reports_the_journey_as_it_happens(platform):
    """The other half of the definition of done: "the timeline and websocket
    reflect it". Driven through the API so the events are the real ones."""
    app, broker, factory, store, manifest = platform
    with TestClient(app):
        client = login(app)
        agg_id = platform_uuid_of(factory)
        # The SOCKET must carry the signed session cookie: an anonymous one
        # is closed with 1008 by design, which is what this used to hit.
        with client.websocket_connect("/api/v1/ws") as socket:
            body = {
                "selection": {"entity_type": "aggregator", "where": {"ids": [agg_id]}},
                "changes": {"logging.verbosity": "warn"},
                "level": "target",
            }
            applied = await asyncio.to_thread(
                client.post,
                f"{API_PREFIX}/config/apply",
                json=body,
                headers={"X-CSRF-Token": client.cookies["eoe_csrf"]},
            )
            assert applied.status_code == 200, applied.text

            event = socket.receive_json()
            assert event["channel"] == "reconciliation"
            assert event["data"]["to_state"] == "pending"
            assert event["entity_id"] == agg_id


async def test_an_edit_survives_a_broker_that_is_down(platform):
    """The failure that matters most here. Publication happens AFTER the
    write commits, so a broker outage costs the publish and not the operator's
    work: the override is saved, the revision is an honest `draft`, and
    `POST /revisions/{id}/publish` is how it goes out later."""
    app, broker, factory, store, manifest = platform
    with TestClient(app):
        client = login(app)
        agg_id = platform_uuid_of(factory)
        broker.stop()
        try:
            body = {
                "selection": {"entity_type": "aggregator", "where": {"ids": [agg_id]}},
                "changes": {"logging.verbosity": "error"},
                "level": "target",
            }
            applied = await asyncio.to_thread(
                client.post,
                f"{API_PREFIX}/config/apply",
                json=body,
                headers={"X-CSRF-Token": client.cookies["eoe_csrf"]},
            )
        finally:
            broker.start()

        assert applied.status_code == 200, "an unreachable broker must not fail the edit"
        result = applied.json()
        assert result["published"] == 0
        assert result["state"] == "draft"
        assert result["revisions"][0]["state"] == "draft"

        # The edit itself is durable, which is the whole point of the ordering.
        effective = client.get(f"{API_PREFIX}/aggregators/{agg_id}/config/effective").json()
        assert effective["config"]["logging.verbosity"]["value"] == "error"

        # And nothing pretended a device had been told. Scoped to THIS apply's
        # revisions: earlier tests in this module legitimately left history.
        mine = {uuid.UUID(r["revision_id"]) for r in result["revisions"]}
        with factory() as db:
            moved = db.scalars(
                select(ReconciliationEvent).where(ReconciliationEvent.revision_id.in_(mine))
            ).all()
        assert moved == [], "a revision that never left the platform moved state anyway"


def test_publication_is_enabled_by_default_now(platform):
    """D61 / task E3.13: the flag flips ON with this task. It stays settable
    per environment for a deployment that wants to stage config without
    touching devices."""
    app, _broker, _factory, _store, _manifest = platform
    assert app.state.settings.publish_enabled is True
    assert (
        Settings(
            database_url="postgresql+psycopg://x/y", session_secret="s", kek=make_kek()
        ).publish_enabled
        is True
    )
