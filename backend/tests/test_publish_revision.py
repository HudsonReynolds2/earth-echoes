"""Gate 43: the desired-config publish path (task E3.4; spec 6.2, 6.4, 7.2, 7.3).

The phase document sets two acceptance criteria, and they are the last two
tests in this file:

* `test_a_device_connecting_afterwards_still_receives_its_desired_config` is
  the spec 6.4 reconnect property. It publishes through the real client
  manager to a real broker, and only THEN connects a subscriber — as the
  Aggregator's own credential, so the broker ACL is on trial too. An
  unretained publish passes every other test in this file and fails this one.
* `test_republishing_the_same_revision_is_idempotent` is the second.

Everything above them runs against a fake publisher that records what it was
handed. That is deliberate: a red test up there means the routing, the state
handling or the transaction is wrong, rather than the broker being slow.

The suite leans on `revision_state` being the only writer of `state` (E3.6)
and asserts states by reading rows back, never by trusting the return value.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from conftest import (
    REPO_ROOT,
    ephemeral_broker,
    ephemeral_postgres,
    free_port,
    make_kek,
)
from sqlalchemy import delete, select, update

from app.auth.passwords import hash_password
from app.config.canonical import config_checksum
from app.contracts.mqtt import (
    QOS,
    DesiredConfig,
    decode,
    desired_topic,
    listener_desired_topic,
)
from app.controlplane.broker import (
    BrokerUnavailable,
    MqttClientManager,
    load_broker_coordinates,
)
from app.controlplane.publisher import (
    AUDIT_ACTION,
    DesiredRoute,
    PublishDisabled,
    StaleRevision,
    UnknownPublishTarget,
    UnknownRevision,
    desired_payload,
    publish_revision,
    resolve_desired_route,
)
from app.controlplane.revision_state import RevisionState, Trigger
from app.db import create_session_factory
from app.devbroker import device_username, load_manifest
from app.models import (
    Aggregator,
    AuditLog,
    ConfigRevision,
    Deployment,
    DeploymentService,
    User,
    utcnow,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy

BACKEND = REPO_ROOT / "backend"

RC = "redwood-coast"
AGG = "demo-agg-rc-01"
#: The first Listener under AGG in the demo hierarchy (seed.DEMO_HIERARCHY).
MAC = "02:EE:0E:01:01:01"

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --- A publisher that records instead of connecting -------------------------


@dataclass(frozen=True)
class Sent:
    deployment_id: uuid.UUID
    topic: str
    payload: bytes
    qos: int
    retain: bool


@dataclass
class FakePublisher:
    """Satisfies `DesiredPublisher` structurally and remembers everything.

    `fails_with` is how the broker-outage test injects a failure at exactly
    the moment the transaction is still open — the only moment where getting
    the ordering wrong is visible.
    """

    fails_with: Exception | None = None
    sent: list[Sent] = field(default_factory=list)

    async def publish(
        self,
        deployment_id: uuid.UUID,
        topic: str,
        payload: bytes,
        *,
        qos: int = QOS,
        retain: bool = False,
    ) -> None:
        if self.fails_with is not None:
            raise self.fails_with
        self.sent.append(Sent(deployment_id, topic, payload, qos, retain))


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def database():
    """One migrated Postgres carrying the demo hierarchy for the whole module.

    Tests make their own revisions and clear them afterwards (`revisions`
    below), so the shared database costs one container start rather than one
    per test while every test still sees only its own rows.
    """
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        yield factory


@pytest.fixture
def revisions(database):
    """The session factory, with revision and audit rows reset around it."""
    yield database
    with database() as db:
        db.execute(delete(ConfigRevision))
        db.execute(delete(AuditLog).where(AuditLog.action == AUDIT_ACTION))
        db.commit()


def deployment_id_of(factory, slug: str = RC) -> uuid.UUID:
    with factory() as db:
        found = db.scalars(select(Deployment.id).where(Deployment.slug == slug)).one()
        return found


def platform_uuid_of(factory, aggregator_uuid: str = AGG) -> str:
    """The Aggregator's PLATFORM UUID (`aggregator.id`) as a string — which is
    what E2 writes into `config_revision.target_id`, and NOT the
    `aggregator_uuid` that appears in the topic (spec 4.2 keeps the three
    identifiers distinct; D75).

    Every aggregator revision in this suite is built through here rather than
    from the literal `AGG`, because a fixture that used the topic segment as
    the target id would agree with a publisher that made the same mistake and
    prove nothing. The real shape came from E2's own apply response.
    """
    with factory() as db:
        found = db.scalars(
            select(Aggregator.id).where(Aggregator.aggregator_uuid == aggregator_uuid)
        ).one()
        return str(found)


SNAPSHOT = {
    "capture.mode": "continuous",
    "capture.sample_rate_hz": 48000,
    "upload.s3_access_key": "secret://upload.s3_access_key",
}


def make_revision(
    factory,
    *,
    target_type: str = "aggregator",
    target_id: str | None = None,
    state: str = "draft",
    snapshot: dict | None = None,
    age: timedelta = timedelta(0),
    created_by: uuid.UUID | None = None,
) -> uuid.UUID:
    """One committed revision, with an EXPLICIT `created_at`.

    The column defaults to `now()`, which Postgres holds constant for a whole
    transaction — two revisions written together would tie, and a test about
    which one is newest would then be deciding it by primary key. `age` makes
    the order the test's own statement.

    `target_id` defaults to the aggregator's platform UUID — see
    `platform_uuid_of`.
    """
    if target_id is None:
        target_id = platform_uuid_of(factory)
    body = SNAPSHOT if snapshot is None else snapshot
    revision = ConfigRevision(
        target_type=target_type,
        target_id=target_id,
        deployment_id=deployment_id_of(factory),
        snapshot=body,
        schema_version=1,
        checksum=config_checksum(body),
        state=state,
        created_by=created_by,
        created_at=utcnow() - age,
    )
    with factory() as db:
        db.add(revision)
        db.commit()
        return revision.id


def make_user(factory) -> uuid.UUID:
    """An operator to attribute a publish to. `audit_log.actor_user_id` is a
    real foreign key, so a made-up id would fail the insert rather than the
    assertion."""
    user = User(
        email=f"publisher-{uuid.uuid4().hex[:10]}@example.com",
        password_hash=hash_password("not-used-here"),
    )
    with factory() as db:
        db.add(user)
        db.commit()
        return user.id


def state_of(factory, revision_id: uuid.UUID) -> str:
    with factory() as db:
        return db.scalars(
            select(ConfigRevision.state).where(ConfigRevision.id == revision_id)
        ).one()


def audit_rows(factory, revision_id: uuid.UUID) -> list[AuditLog]:
    with factory() as db:
        return list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.action == AUDIT_ACTION,
                    AuditLog.entity_id == str(revision_id),
                )
            ).all()
        )


# --- Routing: which topic a revision belongs on (spec 7.2) ------------------


def test_an_aggregator_revision_routes_to_its_own_desired_topic(revisions):
    """The revision carries the platform UUID and the topic carries the
    `aggregator_uuid`; spec 4.2 keeps them distinct and this is the lookup that
    crosses between them (D75). A publisher that used `target_id` as the topic
    segment would build `eoe/redwood-coast/agg/<a uuid>/desired`, which no
    device subscribes to and no ACL grants."""
    revision_id = make_revision(revisions)
    with revisions() as db:
        revision = db.get(ConfigRevision, revision_id)
        assert revision is not None
        assert revision.target_id == platform_uuid_of(revisions)
        assert revision.target_id != AGG
        route = resolve_desired_route(db, revision)
    assert route == DesiredRoute(
        deployment_id=deployment_id_of(revisions),
        slug=RC,
        aggregator_uuid=AGG,
        device_id=AGG,
        topic=desired_topic(RC, AGG),
    )
    assert route.topic == f"eoe/{RC}/agg/{AGG}/desired"


def test_a_listener_revision_routes_to_its_aggregators_subtopic(revisions):
    """Listeners never connect to MQTT (spec 6.4): their desired config hangs
    off the Aggregator that will push it over the local link, so the topic has
    to be resolved through the Listener's Aggregator rather than built from
    the Listener alone."""
    revision_id = make_revision(revisions, target_type="listener", target_id=MAC)
    with revisions() as db:
        revision = db.get(ConfigRevision, revision_id)
        assert revision is not None
        route = resolve_desired_route(db, revision)
    assert route.aggregator_uuid == AGG
    assert route.topic == listener_desired_topic(RC, AGG, MAC)
    assert route.topic == f"eoe/{RC}/agg/{AGG}/lst/{MAC}/desired"


def test_a_revision_whose_device_is_gone_has_nowhere_to_go(revisions):
    """`target_id` is deliberately un-FK'd (D33) so revision history outlives
    the devices it describes. A revision for a decommissioned Aggregator is
    evidence worth keeping and has no topic — a named refusal, not a crash."""
    retired = str(uuid.uuid4())
    revision_id = make_revision(revisions, target_id=retired)
    with revisions() as db:
        revision = db.get(ConfigRevision, revision_id)
        assert revision is not None
        with pytest.raises(UnknownPublishTarget) as raised:
            resolve_desired_route(db, revision)
    assert retired in str(raised.value)


def test_an_aggregator_target_id_that_is_not_a_platform_uuid_is_refused(revisions):
    """The mistake this suite was itself making before E3.4's manual pass:
    putting the `aggregator_uuid` in `target_id`. It is not a lookup that can
    succeed, and the message says which identifier belongs there rather than
    reporting a device that is 'not in inventory' when it plainly is."""
    revision_id = make_revision(revisions, target_id=AGG)
    with revisions() as db:
        revision = db.get(ConfigRevision, revision_id)
        assert revision is not None
        with pytest.raises(UnknownPublishTarget) as raised:
            resolve_desired_route(db, revision)
    assert "aggregator.id" in str(raised.value)


# --- The payload is the snapshot, byte for byte (D52) -----------------------


def test_the_payload_carries_the_snapshot_and_checksum_verbatim(revisions):
    """The stored checksum is the checksum of exactly these bytes, so a device
    that echoes what it applied matches by construction. Rewriting the
    snapshot on the way out would break that match for every device at once
    and surface as fleet-wide phantom drift rather than as an error."""
    revision_id = make_revision(revisions)
    with revisions() as db:
        revision = db.get(ConfigRevision, revision_id)
        assert revision is not None
        route = resolve_desired_route(db, revision)
        payload = desired_payload(revision, route)
        assert payload.config == revision.snapshot
        assert payload.checksum == revision.checksum
        assert payload.revision_id == revision.id
        assert payload.target.type == "aggregator"
        # What the DEVICE calls itself, not the platform's row key (D75): an
        # Aggregator receiving its own primary key could not recognize itself.
        assert payload.target.id == AGG
        assert payload.target.id != revision.target_id
        assert payload.schema_version == 1
        assert payload.generated_at.tzinfo is not None


def test_secret_values_never_reach_the_desired_topic(revisions):
    """Secrets do not transit desired config (spec 5.4, 8). E2 put MARKERS in
    the snapshot; this module has no secret handling of its own and must never
    grow any, so the marker is what goes out."""
    revision_id = make_revision(revisions)
    with revisions() as db:
        revision = db.get(ConfigRevision, revision_id)
        assert revision is not None
        payload = desired_payload(revision, resolve_desired_route(db, revision))
    assert payload.config["upload.s3_access_key"] == "secret://upload.s3_access_key"


# --- The flag (D61) ---------------------------------------------------------


async def test_publishing_is_refused_while_the_flag_is_off(revisions):
    """`EOE_PUBLISH_ENABLED` is checked inside `publish_revision`, not at the
    call sites, so no future caller can reach a device by forgetting it."""
    revision_id = make_revision(revisions)
    publisher = FakePublisher()

    with pytest.raises(PublishDisabled):
        await publish_revision(revisions, publisher, revision_id, publish_enabled=False)

    assert publisher.sent == [], "a disabled publish still reached the broker"
    assert state_of(revisions, revision_id) == "draft"


# --- Publishing a draft -----------------------------------------------------


async def test_publishing_a_draft_makes_it_pending_and_audits_the_actor(revisions):
    actor = make_user(revisions)
    revision_id = make_revision(revisions, created_by=actor)
    publisher = FakePublisher()

    outcome = await publish_revision(
        revisions, publisher, revision_id, publish_enabled=True, actor_user_id=actor
    )

    assert state_of(revisions, revision_id) == "pending"
    assert outcome.transitioned is True
    assert outcome.trigger is Trigger.PUBLISH
    assert outcome.state is RevisionState.PENDING
    rows = audit_rows(revisions, revision_id)
    assert len(rows) == 1
    assert rows[0].actor_user_id == actor, "the publish was not attributed to its operator"
    assert rows[0].scope == deployment_id_of(revisions), "audited outside the deployment scope"
    assert rows[0].detail["from_state"] == "draft"
    assert rows[0].detail["to_state"] == "pending"
    assert rows[0].detail["trigger"] == "publish"
    assert rows[0].detail["topic"] == desired_topic(RC, AGG)
    assert rows[0].detail["checksum"].startswith("sha256:")


async def test_a_system_publish_is_audited_with_no_actor(revisions):
    """`actor_user_id` is None for system-driven work — the `record_audit`
    convention, which E3.7's timeout sweep and E3.5's reports also follow, so
    every surface agrees on what "no operator" looks like."""
    revision_id = make_revision(revisions)

    await publish_revision(revisions, FakePublisher(), revision_id, publish_enabled=True)

    assert audit_rows(revisions, revision_id)[0].actor_user_id is None


async def test_the_message_is_retained_and_at_qos_1(revisions):
    """Spec 7.2 marks the desired topics retained and spec 6.4 depends on it.
    QoS 1 is the phase-3 fixed choice for every platform topic."""
    revision_id = make_revision(revisions)
    publisher = FakePublisher()

    await publish_revision(revisions, publisher, revision_id, publish_enabled=True)

    assert len(publisher.sent) == 1
    sent = publisher.sent[0]
    assert sent.retain is True
    assert sent.qos == QOS == 1
    assert sent.topic == desired_topic(RC, AGG)
    assert sent.deployment_id == deployment_id_of(revisions)
    body = decode(DesiredConfig, sent.payload)
    assert body.revision_id == revision_id
    assert body.config == SNAPSHOT


async def test_a_listener_revision_publishes_to_the_listener_subtopic(revisions):
    revision_id = make_revision(revisions, target_type="listener", target_id=MAC)
    publisher = FakePublisher()

    await publish_revision(revisions, publisher, revision_id, publish_enabled=True)

    assert publisher.sent[0].topic == listener_desired_topic(RC, AGG, MAC)
    assert publisher.sent[0].retain is True
    body = decode(DesiredConfig, publisher.sent[0].payload)
    assert body.target.type == "listener"
    assert body.target.id == MAC


# --- The trigger follows the source state (spec 6.2) ------------------------


async def test_a_drifted_revision_republishes_under_the_republish_trigger(revisions):
    """Spec 6.2 reaches `pending` by three edges and they are three different
    events on the timeline. A hardcoded `publish` trigger would be rejected by
    the state machine here rather than silently mislabelled — which is why the
    machine validates triples."""
    revision_id = make_revision(revisions, state="drifted")
    outcome = await publish_revision(revisions, FakePublisher(), revision_id, publish_enabled=True)
    assert outcome.trigger is Trigger.REPUBLISH
    assert state_of(revisions, revision_id) == "pending"
    assert audit_rows(revisions, revision_id)[0].detail["trigger"] == "republish"


async def test_a_failed_revision_retries_under_the_retry_trigger(revisions):
    revision_id = make_revision(revisions, state="failed")
    outcome = await publish_revision(revisions, FakePublisher(), revision_id, publish_enabled=True)
    assert outcome.trigger is Trigger.RETRY
    assert state_of(revisions, revision_id) == "pending"


# --- Idempotency (the second acceptance criterion) --------------------------


async def test_republishing_the_same_revision_is_idempotent(revisions):
    """ACCEPTANCE (phase doc E3.4).

    The second call re-sends byte-identical bytes to the same retained topic,
    moves no state and writes no second audit row. Re-sending rather than
    returning early is deliberate: it is the operator's repair for a broker
    that lost its retained store, and it is safe precisely because the bytes
    are identical — a device holding them already sees a matching checksum.
    """
    revision_id = make_revision(revisions)
    publisher = FakePublisher()

    first = await publish_revision(revisions, publisher, revision_id, publish_enabled=True)
    second = await publish_revision(revisions, publisher, revision_id, publish_enabled=True)

    assert first.transitioned is True
    assert second.transitioned is False, "the second publish moved state a second time"
    assert second.trigger is None
    assert second.state is RevisionState.PENDING
    assert state_of(revisions, revision_id) == "pending"
    assert len(audit_rows(revisions, revision_id)) == 1, "one publish, two audit rows"

    assert len(publisher.sent) == 2
    one, two = publisher.sent
    assert one.topic == two.topic
    # Byte equality except for `generated_at`, which is when it was sent.
    first_body = decode(DesiredConfig, one.payload)
    second_body = decode(DesiredConfig, two.payload)
    assert first_body.config == second_body.config
    assert first_body.checksum == second_body.checksum
    assert first_body.revision_id == second_body.revision_id
    assert two.retain is True


async def test_an_applied_revision_can_be_resent_without_leaving_applied(revisions):
    """The retained-store repair again, from the state a healthy device sits
    in. Moving `applied` back to `pending` for a re-send would make the device
    look unreconciled when nothing about it changed."""
    revision_id = make_revision(revisions, state="applied")
    publisher = FakePublisher()

    outcome = await publish_revision(revisions, publisher, revision_id, publish_enabled=True)

    assert outcome.transitioned is False
    assert outcome.state is RevisionState.APPLIED
    assert state_of(revisions, revision_id) == "applied"
    assert len(publisher.sent) == 1


# --- Superseding, and the rule that makes it safe ---------------------------


async def test_publishing_supersedes_every_other_open_revision_for_the_device(revisions):
    """From the moment the retained topic carries this revision, no other
    revision for that device is the desired state — whatever state it was left
    in (spec 6.2's diagram; D69)."""
    older_applied = make_revision(revisions, state="applied", age=timedelta(minutes=30))
    older_failed = make_revision(revisions, state="failed", age=timedelta(minutes=20))
    older_draft = make_revision(revisions, state="draft", age=timedelta(minutes=10))
    newest = make_revision(revisions)
    other_device = make_revision(revisions, target_id=platform_uuid_of(revisions, "demo-agg-rc-02"))

    outcome = await publish_revision(revisions, FakePublisher(), newest, publish_enabled=True)

    assert state_of(revisions, newest) == "pending"
    for older in (older_applied, older_failed, older_draft):
        assert state_of(revisions, older) == "superseded"
    assert set(outcome.superseded) == {older_applied, older_failed, older_draft}
    assert state_of(revisions, other_device) == "draft", "another device's revision was touched"


async def test_an_older_revision_is_refused_so_a_newer_draft_survives(revisions):
    """The pair rule. `supersede_open_revisions` is unconditional on
    timestamps, which is only safe because publishing a superseded-by-age
    revision is refused HERE. Removing either rule alone silently discards a
    newer draft an operator was still working on."""
    older = make_revision(revisions, age=timedelta(minutes=5))
    newer_draft = make_revision(revisions)
    publisher = FakePublisher()

    with pytest.raises(StaleRevision) as raised:
        await publish_revision(revisions, publisher, older, publish_enabled=True)

    assert str(newer_draft) in str(raised.value)
    assert publisher.sent == []
    assert state_of(revisions, older) == "draft"
    assert state_of(revisions, newer_draft) == "draft", "the newer draft was superseded anyway"


async def test_a_superseded_revision_is_never_republished(revisions):
    """`superseded` is spec 6.2's only terminal state. Re-arming one would
    undo the revision that replaced it."""
    revision_id = make_revision(revisions, state="superseded")
    publisher = FakePublisher()

    with pytest.raises(StaleRevision):
        await publish_revision(revisions, publisher, revision_id, publish_enabled=True)

    assert publisher.sent == []
    assert state_of(revisions, revision_id) == "superseded"


# --- Failure leaves nothing half-done ---------------------------------------


async def test_a_broker_outage_leaves_the_revision_in_draft_with_nothing_published(revisions):
    """The publish happens INSIDE the transaction, so a broker failure rolls
    the whole thing back. The alternative — commit `pending`, then fail to
    publish — resolves as a spec 6.2 timeout and blames the device for the
    platform's failure."""
    older = make_revision(revisions, state="applied", age=timedelta(minutes=5))
    revision_id = make_revision(revisions)
    publisher = FakePublisher(fails_with=BrokerUnavailable("no live connection"))

    with pytest.raises(BrokerUnavailable):
        await publish_revision(revisions, publisher, revision_id, publish_enabled=True)

    assert state_of(revisions, revision_id) == "draft"
    assert state_of(revisions, older) == "applied", "a rolled-back publish still superseded"
    assert audit_rows(revisions, revision_id) == [], "an audit row for a publish that never was"


async def test_an_unknown_revision_id_is_a_named_failure(revisions):
    publisher = FakePublisher()
    with pytest.raises(UnknownRevision):
        await publish_revision(revisions, publisher, uuid.uuid4(), publish_enabled=True)
    assert publisher.sent == []


# --- ACCEPTANCE: the spec 6.4 reconnect property, against a real broker -----


def _provision(url: str, kek: str, out) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--out", str(out), "--host", "127.0.0.1"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": url,
            "EOE_SESSION_SECRET": f"publisher-{uuid.uuid4().hex}",
            "EOE_KEK": kek,
        },
        timeout=180,
    )
    assert result.returncode == 0, f"devbroker failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def live_stack(tmp_path_factory):
    """A provisioned broker plus its own database, for the acceptance test.

    Separate from the `database` fixture above because this one needs the
    broker rows pointing at a real port, and the tests above must not pay for
    a Mosquitto container to assert routing arithmetic.
    """
    out = tmp_path_factory.mktemp("publisher-certs")
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


async def test_a_device_connecting_afterwards_still_receives_its_desired_config(live_stack):
    """ACCEPTANCE (phase doc E3.4), and the spec 6.4 reconnect property.

    The revision is published while nothing is subscribed. Only afterwards
    does a client connect — as the Aggregator's OWN dev credential, so the
    spec 7.1 ACL is on trial with it — and it must still be handed the desired
    config, because the broker retained it. An unretained publish passes every
    other test in this file and fails this one.
    """
    broker, factory, store, manifest = live_stack
    revision_id = make_revision(factory)

    coords = load_broker_coordinates(factory, store)
    deployment_id = next(c.deployment_id for c in coords if c.slug == RC)
    manager = MqttClientManager(lambda: coords)
    async with manager:
        await manager.wait_connected(deployment_id)
        outcome = await publish_revision(factory, manager, revision_id, publish_enabled=True)
    assert outcome.topic == desired_topic(RC, AGG)

    # Nothing was listening until now.
    username = device_username(AGG)
    password = next(a["password"] for a in manifest["accounts"] if a["username"] == username)
    subscribed = await asyncio.to_thread(
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
        desired_topic(RC, AGG),
        "-W",
        "10",
        "-C",
        "1",
    )
    assert subscribed.returncode == 0, subscribed.stderr

    body = decode(DesiredConfig, subscribed.stdout.strip())
    assert body.revision_id == revision_id
    assert body.config == SNAPSHOT
    assert body.target.id == AGG
    with factory() as db:
        assert (
            db.scalars(select(ConfigRevision.state).where(ConfigRevision.id == revision_id)).one()
            == "pending"
        )
