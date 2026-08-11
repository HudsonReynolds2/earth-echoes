"""Gate 44: the reported consumer (task E3.5; spec 4.3, 6.1, 6.2, 7.3, 7.4).

The phase document sets three acceptance criteria and they are named as such
below: a replayed message test, a reordered message test, and a conflicting
MAC report that lands in quarantine **with inventory rows provably
unchanged** — proven by reading the Listener row before and after and
comparing it to itself, not by trusting that no write was attempted.

Almost everything here drives `ReportedConsumer.consume` directly with a
hand-built `InboundMessage`, which is what a broker delivers. That is
deliberate: a red test up here means the routing, the identity handling or a
spec 6.2 edge is wrong, rather than a container being slow. The last test in
the file closes the loop against a real Mosquitto, publishing as the device's
own credential so the spec 7.1 ACL and the subscription filters are on trial
too.

States are asserted by reading rows back, never from the return value alone —
the suite leans on `revision_state.transition` being the only writer of
`config_revision.state` (E3.6) and checks that nothing else moved it.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    REPO_ROOT,
    ephemeral_broker,
    ephemeral_postgres,
    free_port,
    make_kek,
)
from sqlalchemy import delete, select, update

from app.config.canonical import config_checksum
from app.contracts.mqtt import (
    EVENT_LISTENER_STREAM_GAP,
    HealthBlock,
    ListenerLiveness,
    ReportedAggregatorState,
    ReportedListenerState,
    StatusMessage,
    encode,
    event_topic,
    listener_reported_topic,
    reported_topic,
    status_topic,
)
from app.contracts.mqtt import (
    DeviceEvent as DeviceEventPayload,
)
from app.controlplane.broker import (
    InboundMessage,
    MqttClientManager,
    load_broker_coordinates,
)
from app.controlplane.consumer import (
    AUDIT_ACTION,
    QUARANTINE_UNKNOWN_MAC,
    ReportedConsumer,
    ReportOutcome,
    delete_device_state_for,
    differing_keys,
    latest_state,
)
from app.controlplane.revision_state import RevisionState
from app.db import create_session_factory
from app.devbroker import device_username, load_manifest
from app.inventory.identity import ALERT_DUPLICATE_IDENTITY, ALERT_PROVISIONING_REQUIRED
from app.models import (
    Aggregator,
    AggregatorStatus,
    AuditLog,
    ConfigRevision,
    Deployment,
    DeploymentService,
    DeviceEvent,
    DeviceState,
    InventoryAlert,
    Listener,
    QuarantinedReport,
    utcnow,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy

BACKEND = REPO_ROOT / "backend"

RC = "redwood-coast"
AGG = "demo-agg-rc-01"
#: A second Aggregator in the same deployment — the other half of the MAC
#: conflict: one device reporting a Listener that belongs to another.
OTHER_AGG = "demo-agg-rc-02"
#: The first Listener under AGG in the demo hierarchy (seed.DEMO_HIERARCHY).
MAC = "02:EE:0E:01:01:01"
#: In no inventory row anywhere (D76's unregistered-Listener case).
STRANGER_MAC = "02:EE:0E:FF:FF:FF"

SNAPSHOT = {
    "capture.mode": "continuous",
    "capture.sample_rate_hz": 48000,
    "upload.s3_access_key": "secret://upload.s3_access_key",
}
#: The same config with one value changed — a device that coherently applied
#: something other than the revision it was given (D70's second case).
DIVERGED = {**SNAPSHOT, "capture.sample_rate_hz": 22050}

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def database():
    """One migrated Postgres carrying the demo hierarchy for the whole module."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        yield factory


@pytest.fixture
def reports(database):
    """The session factory, with everything a report can write reset after it."""
    yield database
    with database() as db:
        for table in (
            ConfigRevision,
            DeviceState,
            DeviceEvent,
            QuarantinedReport,
            InventoryAlert,
            AggregatorStatus,  # E3.8 writes here from this module's status test
        ):
            db.execute(delete(table))
        db.execute(
            delete(AuditLog).where(
                AuditLog.action.in_([AUDIT_ACTION, "inventory.alert", "inventory.quarantine"])
            )
        )
        db.commit()


@pytest.fixture
def consumer(reports) -> ReportedConsumer:
    return ReportedConsumer(reports)


def deployment_id_of(factory, slug: str = RC) -> uuid.UUID:
    with factory() as db:
        return db.scalars(select(Deployment.id).where(Deployment.slug == slug)).one()


def platform_uuid_of(factory, aggregator_uuid: str = AGG) -> str:
    """The Aggregator's PLATFORM UUID (`aggregator.id`) as a string — what E2
    writes into `config_revision.target_id`, and NOT the `aggregator_uuid` in
    the topic (D75). Derived from live inventory for the same reason E3.4's
    suite does it: a fixture that invented the id shape would agree with an
    implementation that made the same mistake."""
    with factory() as db:
        return str(
            db.scalars(
                select(Aggregator.id).where(Aggregator.aggregator_uuid == aggregator_uuid)
            ).one()
        )


def make_revision(
    factory,
    *,
    target_type: str = "aggregator",
    target_id: str | None = None,
    state: str = "pending",
    snapshot: dict | None = None,
    age: timedelta = timedelta(0),
) -> uuid.UUID:
    """One committed revision. Defaults to `pending`, which is the state a
    report normally finds: E3.4 published it and the device is answering."""
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
        created_at=utcnow() - age,
    )
    with factory() as db:
        db.add(revision)
        db.commit()
        return revision.id


def state_of(factory, revision_id: uuid.UUID) -> str:
    with factory() as db:
        return db.scalars(
            select(ConfigRevision.state).where(ConfigRevision.id == revision_id)
        ).one()


def stored_state(factory, entity_type: str, entity_id: str) -> DeviceState | None:
    with factory() as db:
        return latest_state(db, entity_type, entity_id)


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


def alerts_of(factory, alert_type: str) -> list[InventoryAlert]:
    with factory() as db:
        return list(
            db.scalars(select(InventoryAlert).where(InventoryAlert.alert_type == alert_type)).all()
        )


def quarantined(factory) -> list[QuarantinedReport]:
    with factory() as db:
        return list(db.scalars(select(QuarantinedReport)).all())


def listener_row(factory, mac: str = MAC) -> dict:
    """A Listener's identity columns as a plain dict, so a before/after
    comparison survives the session that read it. Spec 4.3 item 2 is a claim
    about the ROW, and a detached ORM object cannot make it."""
    with factory() as db:
        row = db.get(Listener, mac)
        assert row is not None
        return {
            "mac": row.mac,
            "name": row.name,
            "aggregator_id": row.aggregator_id,
            "deployment_id": row.deployment_id,
            "updated_at": row.updated_at,
        }


# --- Building what a broker delivers ----------------------------------------


def inbound(factory, topic: str, payload: bytes, *, slug: str = RC) -> InboundMessage:
    """One delivered message. `deployment_id` is the CONNECTION's deployment —
    the manager knows which broker it dialled, and that is what the consumer
    checks a device's inventory home against."""
    return InboundMessage(
        deployment_id=deployment_id_of(factory, slug),
        deployment_slug=slug,
        topic=topic,
        payload=payload,
        qos=1,
        retain=False,
    )


def aggregator_report(
    factory,
    *,
    revision_id: uuid.UUID | None,
    config: dict | None = None,
    at: datetime | None = None,
    agg: str = AGG,
    checksum: str | None = None,
    health: HealthBlock | None = None,
) -> InboundMessage:
    body = SNAPSHOT if config is None else config
    payload = ReportedAggregatorState(
        reported_at=at or utcnow(),
        applied_revision_id=revision_id,
        config=body,
        checksum=checksum or config_checksum(body),
        health=health,
    )
    return inbound(factory, reported_topic(RC, agg), encode(payload))


def listener_report(
    factory,
    *,
    revision_id: uuid.UUID | None,
    mac: str = MAC,
    agg: str = AGG,
    config: dict | None = None,
    at: datetime | None = None,
) -> InboundMessage:
    body = SNAPSHOT if config is None else config
    payload = ReportedListenerState(
        reported_at=at or utcnow(),
        applied_revision_id=revision_id,
        config=body,
        checksum=config_checksum(body),
        liveness=ListenerLiveness(state="streaming"),
    )
    return inbound(factory, listener_reported_topic(RC, agg, mac), encode(payload))


def device_event(
    factory,
    *,
    code: str = EVENT_LISTENER_STREAM_GAP,
    at: datetime | None = None,
    mac: str | None = MAC,
    agg: str = AGG,
    detail: str | None = "listener gap 240ms",
) -> InboundMessage:
    payload = DeviceEventPayload(
        at=at or utcnow(),
        level="warn",
        code=code,
        detail=detail,
        listener_mac=mac,
    )
    return inbound(factory, event_topic(RC, agg), encode(payload))


# --- The boundary: what is not a reconciliation outcome ---------------------


def test_a_status_message_is_not_a_reported_state(consumer, reports):
    """E3.8 claimed the status topic (this test previously asserted the E3.5
    seam, `NOT_MINE`). The boundary it now guards is the one that still
    matters: a status message says whether the device is REACHABLE, and never
    a word about what config it is running. It must not create or touch a
    `device_state` row — `test_lwt_status.py` owns the rest of the behaviour.
    """
    payload = encode(StatusMessage(state="online", at=datetime.now(UTC)))
    message = inbound(reports, status_topic(RC, AGG), payload)
    assert consumer.consume(message) is ReportOutcome.ONLINE
    assert stored_state(reports, "aggregator", platform_uuid_of(reports)) is None


def test_an_undecodable_payload_moves_nothing(consumer, reports):
    """One device's malformed report must not become a reconciliation outcome.
    `PayloadError` is caught at the boundary and the revision is left for the
    timeout, whose message is then still true (D70)."""
    revision_id = make_revision(reports)
    message = inbound(reports, reported_topic(RC, AGG), b"{not json at all")

    assert consumer.consume(message) is ReportOutcome.MALFORMED

    assert state_of(reports, revision_id) == "pending"
    assert stored_state(reports, "aggregator", platform_uuid_of(reports)) is None


def test_a_device_whose_checksum_is_not_its_own_configs_is_rejected(consumer, reports):
    """D70's first case: the device contradicts ITSELF, so the message is
    malformed rather than a report of the wrong config. No transition — an
    unparseable report is not evidence about whether the config was applied,
    and the platform recomputes the checksum rather than trusting a naked field
    because that is what makes the D52 echo-match a property and not a hope."""
    revision_id = make_revision(reports)
    message = aggregator_report(
        reports, revision_id=revision_id, config=SNAPSHOT, checksum=config_checksum(DIVERGED)
    )

    assert consumer.consume(message) is ReportOutcome.MALFORMED

    assert state_of(reports, revision_id) == "pending", "a self-contradicting report moved state"
    assert stored_state(reports, "aggregator", platform_uuid_of(reports)) is None


# --- Identity: spec 4.3, through the E1.5 services --------------------------


def test_a_report_from_an_unprovisioned_aggregator_raises_the_alert(consumer, reports):
    """Spec 4.3 item 3: unprovisioned detection is a MEMBERSHIP check, and the
    consumer fires it off the E1.5 service rather than reimplementing it. The
    alert is what an operator sees; nothing joins to a device that is not in
    inventory."""
    stranger = "not-in-inventory-at-all"
    message = inbound(
        reports,
        reported_topic(RC, stranger),
        encode(
            ReportedAggregatorState(
                reported_at=utcnow(), config=SNAPSHOT, checksum=config_checksum(SNAPSHOT)
            )
        ),
    )

    assert consumer.consume(message) is ReportOutcome.UNPROVISIONED

    opened = alerts_of(reports, ALERT_PROVISIONING_REQUIRED)
    assert len(opened) == 1
    assert opened[0].entity_key == stranger
    assert stored_state(reports, "aggregator", stranger) is None


def test_a_conflicting_mac_report_is_quarantined_and_inventory_is_untouched(consumer, reports):
    """ACCEPTANCE (phase doc E3.5).

    A Listener that belongs to `demo-agg-rc-01` reported by `demo-agg-rc-02` —
    two devices claiming one MAC, or one device on the wrong parent. Spec 4.3
    item 2 says the platform quarantines the REPORT "rather than overwriting
    inventory", so the Listener row is captured before and compared to itself
    afterwards: unchanged, field for field, including `updated_at`.
    """
    revision_id = make_revision(reports, target_type="listener", target_id=MAC)
    before = listener_row(reports)

    outcome = consumer.consume(listener_report(reports, revision_id=revision_id, agg=OTHER_AGG))

    assert outcome is ReportOutcome.QUARANTINED
    assert listener_row(reports) == before, "a conflicting report modified inventory"

    rows = quarantined(reports)
    assert len(rows) == 1
    assert rows[0].mac == MAC
    assert rows[0].reason == "mac_conflict"
    assert rows[0].aggregator_uuid == OTHER_AGG
    assert len(alerts_of(reports, ALERT_DUPLICATE_IDENTITY)) == 1

    # And it is not believed as state either: a report the platform rejects
    # must not become the device's reported configuration or move its revision.
    assert stored_state(reports, "listener", MAC) is None
    assert state_of(reports, revision_id) == "pending"


def test_an_unregistered_listener_is_quarantined_without_an_alert(consumer, reports):
    """D76. A known Aggregator reporting a MAC in no inventory row is not a
    conflict — E1.5 correctly has no opinion — but it is evidence an operator
    should be able to find and adopt, so the reported channel quarantines it.
    No `duplicate_identity` alert: nothing disagrees with anything."""
    outcome = consumer.consume(listener_report(reports, revision_id=None, mac=STRANGER_MAC))

    assert outcome is ReportOutcome.QUARANTINED
    rows = quarantined(reports)
    assert len(rows) == 1
    assert rows[0].mac == STRANGER_MAC
    assert rows[0].reason == QUARANTINE_UNKNOWN_MAC
    assert alerts_of(reports, ALERT_DUPLICATE_IDENTITY) == []
    assert stored_state(reports, "listener", STRANGER_MAC) is None


def test_a_device_reporting_on_another_deployments_broker_is_dropped(consumer, reports):
    """The broker ACL should make this impossible (spec 7.1), so it is a
    refusal rather than a quarantine: a device holding a credential for a
    namespace it does not belong to is a broker problem, not an inventory one.
    The connection's deployment is authoritative — the platform dialled it."""
    revision_id = make_revision(reports)
    other = deployment_id_of(reports, "high-desert")
    message = aggregator_report(reports, revision_id=revision_id)
    message = InboundMessage(
        deployment_id=other,
        deployment_slug="high-desert",
        topic=message.topic,
        payload=message.payload,
        qos=message.qos,
        retain=message.retain,
    )

    assert consumer.consume(message) is ReportOutcome.MISROUTED

    assert state_of(reports, revision_id) == "pending"
    assert stored_state(reports, "aggregator", platform_uuid_of(reports)) is None


def test_a_report_naming_another_devices_revision_is_refused(consumer, reports):
    """`applied_revision_id` is device-supplied. A revision belongs to exactly
    one device, so a report claiming someone else's says nothing reliable about
    either — storing it would launder the confusion into the record."""
    theirs = make_revision(reports, target_id=platform_uuid_of(reports, OTHER_AGG))

    assert consumer.consume(aggregator_report(reports, revision_id=theirs)) is (
        ReportOutcome.MISROUTED
    )

    assert state_of(reports, theirs) == "pending"
    assert stored_state(reports, "aggregator", platform_uuid_of(reports)) is None


# --- The spec 6.2 edges a report can drive ----------------------------------


def test_a_matching_report_moves_a_pending_revision_to_applied(consumer, reports):
    """Spec 6.2's `pending -> applied`, "device reports matching state" — the
    happy path of the whole control plane."""
    revision_id = make_revision(reports)

    assert consumer.consume(aggregator_report(reports, revision_id=revision_id)) is (
        ReportOutcome.APPLIED
    )

    assert state_of(reports, revision_id) == "applied"
    rows = audit_rows(reports, revision_id)
    assert len(rows) == 1
    assert rows[0].actor_user_id is None, "a device report is system-originated"
    assert rows[0].scope == deployment_id_of(reports)
    assert rows[0].detail["from_state"] == "pending"
    assert rows[0].detail["to_state"] == "applied"
    assert rows[0].detail["trigger"] == "report_match"


def test_a_coherent_report_of_the_wrong_config_fails_the_revision_at_once(consumer, reports):
    """D70. The device answered, and answered wrong: a definite negative, so
    the revision fails NOW rather than waiting out the 300-second window and
    reporting a timeout for a device that replied in two seconds. That is what
    keeps `failed(timeout)` meaning silence and nothing else."""
    revision_id = make_revision(reports)

    outcome = consumer.consume(aggregator_report(reports, revision_id=revision_id, config=DIVERGED))

    assert outcome is ReportOutcome.REJECTED
    assert state_of(reports, revision_id) == "failed"
    detail = audit_rows(reports, revision_id)[0].detail
    assert detail["trigger"] == "report_error"
    assert detail["differing_keys"] == ["capture.sample_rate_hz"]


def test_a_mismatch_detail_names_keys_and_never_values(consumer, reports):
    """Rule R2. Snapshots hold secret markers and device-supplied values are of
    unknown provenance, so the operator gets the key NAMES that disagree and
    reads the values from the revision and the reported state."""
    secret_marker = {**SNAPSHOT, "network.wifi_password": {"$secret": "config:pod:x:wifi"}}
    revision_id = make_revision(reports, snapshot=secret_marker)
    reported = {**secret_marker, "network.wifi_password": "hunter2-in-the-clear"}

    consumer.consume(aggregator_report(reports, revision_id=revision_id, config=reported))

    detail = json.dumps(audit_rows(reports, revision_id)[0].detail)
    assert "network.wifi_password" in detail
    assert "hunter2-in-the-clear" not in detail
    assert "$secret" not in detail


def test_a_divergent_report_drifts_an_applied_revision(consumer, reports):
    """Spec 6.2's `applied -> drifted`: the device had it and no longer does.
    Drift never auto-republishes in this phase — it waits for an operator."""
    revision_id = make_revision(reports, state="applied")

    outcome = consumer.consume(aggregator_report(reports, revision_id=revision_id, config=DIVERGED))

    assert outcome is ReportOutcome.DRIFTED
    assert state_of(reports, revision_id) == "drifted"
    assert audit_rows(reports, revision_id)[0].detail["trigger"] == "report_diverged"


@pytest.mark.parametrize("resting", ["draft", "drifted", "failed", "superseded"])
def test_no_report_moves_a_revision_spec_6_2_gives_no_edge_from(consumer, reports, resting):
    """The four states a device report cannot move. `drifted` and `failed` wait
    on an operator, `draft` was never published so nothing can have applied it,
    and `superseded` is spec 7.4's stale-report case decided by the publish that
    closed it. Checked BEFORE the state machine is asked, because offering it an
    illegal triple would raise where nothing is wrong."""
    revision_id = make_revision(reports, state=resting)

    outcome = consumer.consume(aggregator_report(reports, revision_id=revision_id))

    assert outcome is ReportOutcome.UNCHANGED
    assert state_of(reports, revision_id) == resting
    assert audit_rows(reports, revision_id) == []
    # Still stored: the device is telling us what it currently runs, which is
    # what E3.7's drift sweep reads even when no revision moves.
    assert stored_state(reports, "aggregator", platform_uuid_of(reports)) is not None


def test_a_device_that_has_applied_nothing_still_reports_its_state(consumer, reports):
    """`applied_revision_id` is optional (D67): a device fresh out of the box
    has state worth recording and no revision to move."""
    assert consumer.consume(aggregator_report(reports, revision_id=None)) is (
        ReportOutcome.UNCHANGED
    )

    stored = stored_state(reports, "aggregator", platform_uuid_of(reports))
    assert stored is not None
    assert stored.applied_revision_id is None


def test_a_listener_report_advances_its_own_revision(consumer, reports):
    """Listeners never hold an MQTT session (spec 6.4): the Aggregator reports
    on their behalf on the Listener subtopic, and the revision it acks is the
    Listener's, addressed by MAC."""
    revision_id = make_revision(reports, target_type="listener", target_id=MAC)

    assert consumer.consume(listener_report(reports, revision_id=revision_id)) is (
        ReportOutcome.APPLIED
    )

    assert state_of(reports, revision_id) == "applied"
    stored = stored_state(reports, "listener", MAC)
    assert stored is not None
    assert stored.deployment_id == deployment_id_of(reports)


# --- ACCEPTANCE: replay and reordering (spec 7.4) ---------------------------


def test_a_replayed_report_changes_nothing_the_second_time(consumer, reports):
    """ACCEPTANCE (phase doc E3.5), the replay half.

    The same bytes twice. QoS 1 is at-least-once, so this is a normal event,
    not a fault. Idempotency comes from `applied_revision_id` plus checksum as
    spec 7.4 words it — the second delivery runs the whole comparison and finds
    the revision already `applied` with a matching checksum — rather than from
    a timestamp shortcut that would hide a broken comparison behind an early
    return. Note the two messages share a `reported_at` exactly.
    """
    revision_id = make_revision(reports)
    message = aggregator_report(reports, revision_id=revision_id, at=utcnow())

    first = consumer.consume(message)
    second = consumer.consume(message)

    assert first is ReportOutcome.APPLIED
    assert second is ReportOutcome.UNCHANGED, "a replay was treated as new news"
    assert state_of(reports, revision_id) == "applied"
    assert len(audit_rows(reports, revision_id)) == 1, "one transition, two audit rows"
    with reports() as db:
        assert db.scalar(select(DeviceState.id).where(DeviceState.entity_type == "aggregator"))
        assert len(list(db.scalars(select(DeviceState)).all())) == 1


def test_an_out_of_order_report_cannot_undo_a_healthy_device(consumer, reports):
    """ACCEPTANCE (phase doc E3.5), the reordering half.

    A report from `t` arrives after one from `t+10`. Spec 7.4 tolerates
    out-of-order delivery by ignoring stale reports, and this is why it
    matters: the late message describes a world that has already moved on, and
    acting on it would drive a reconciled device to `drifted` on the strength
    of news that was true ten seconds ago. Dropped WHOLE — no transition and no
    stored state, so the record still says what the device last actually said.
    """
    revision_id = make_revision(reports)
    now = utcnow()

    fresh = consumer.consume(aggregator_report(reports, revision_id=revision_id, at=now))
    late = consumer.consume(
        aggregator_report(
            reports, revision_id=revision_id, config=DIVERGED, at=now - timedelta(seconds=10)
        )
    )

    assert fresh is ReportOutcome.APPLIED
    assert late is ReportOutcome.STALE
    assert state_of(reports, revision_id) == "applied", "a stale report drifted a healthy device"
    stored = stored_state(reports, "aggregator", platform_uuid_of(reports))
    assert stored is not None
    assert stored.reported_at == now, "the stale report overwrote the newer one"
    assert stored.config == SNAPSHOT
    assert len(audit_rows(reports, revision_id)) == 1


def test_a_late_report_for_a_superseded_revision_does_not_disturb_the_pending_one(
    consumer, reports
):
    """The reordering case that matters in the field: the device acks the
    revision it had while a newer one is already published and waiting. The old
    revision is `superseded` and terminal, the new one stays `pending`, and the
    device's current state is recorded honestly as not-yet-caught-up."""
    old = make_revision(reports, state="superseded", age=timedelta(minutes=5))
    new = make_revision(reports, state="pending", snapshot=DIVERGED)

    assert consumer.consume(aggregator_report(reports, revision_id=old)) is ReportOutcome.UNCHANGED

    assert state_of(reports, old) == "superseded"
    assert state_of(reports, new) == "pending"
    stored = stored_state(reports, "aggregator", platform_uuid_of(reports))
    assert stored is not None
    assert stored.applied_revision_id == old


# --- What a report leaves behind (spec 6.1) ---------------------------------


def test_the_reported_state_is_stored_as_spec_6_1s_reported_configuration(consumer, reports):
    """Spec 6.1's other half: every device carries a desired configuration and
    a reported one. E3.7's drift sweep re-compares this row against the desired
    snapshot without waiting for a new report."""
    revision_id = make_revision(reports)
    at = utcnow()

    consumer.consume(
        aggregator_report(
            reports,
            revision_id=revision_id,
            at=at,
            health=HealthBlock(uptime_s=86400, coarse="ok"),
        )
    )

    stored = stored_state(reports, "aggregator", platform_uuid_of(reports))
    assert stored is not None
    assert stored.entity_id == platform_uuid_of(reports), "stored under the wrong identifier (D75)"
    assert stored.applied_revision_id == revision_id
    assert stored.config == SNAPSHOT
    assert stored.checksum == config_checksum(SNAPSHOT)
    assert stored.reported_at == at
    assert stored.health == {"uptime_s": 86400, "coarse": "ok"}
    assert stored.deployment_id == deployment_id_of(reports)


def test_a_later_report_replaces_the_stored_one_rather_than_adding_a_row(consumer, reports):
    """One row per device: this is current state, not history. The append-only
    record of what a device said is `device_event` and the revision
    transitions."""
    revision_id = make_revision(reports)
    now = utcnow()

    consumer.consume(aggregator_report(reports, revision_id=revision_id, at=now))
    consumer.consume(
        aggregator_report(
            reports, revision_id=revision_id, config=DIVERGED, at=now + timedelta(seconds=30)
        )
    )

    with reports() as db:
        rows = list(db.scalars(select(DeviceState)).all())
    assert len(rows) == 1
    assert rows[0].config == DIVERGED


def test_deleting_a_device_takes_its_reported_state_with_it(consumer, reports):
    """`delete_device_state_for`, the `delete_overrides_for` precedent. A
    Listener re-added under a MAC that once belonged to another physical device
    would otherwise inherit its predecessor's reported config and read as
    reconciled before it had said a word."""
    revision_id = make_revision(reports, target_type="listener", target_id=MAC)
    consumer.consume(listener_report(reports, revision_id=revision_id))
    assert stored_state(reports, "listener", MAC) is not None

    with reports() as db:
        delete_device_state_for(db, "listener", MAC)
        db.commit()

    assert stored_state(reports, "listener", MAC) is None


# --- Events (spec 7.3) ------------------------------------------------------


def test_an_event_is_persisted_with_the_emitter_the_broker_authenticated(consumer, reports):
    """`aggregator_uuid` comes from the topic, which the spec 7.1 ACL cuts to
    one device's subtree — never from a payload field, which would be a
    self-declaration any device could write anything into."""
    at = utcnow()

    assert consumer.consume(device_event(reports, at=at)) is ReportOutcome.EVENT

    with reports() as db:
        rows = list(db.scalars(select(DeviceEvent)).all())
    assert len(rows) == 1
    assert rows[0].aggregator_uuid == AGG
    assert rows[0].listener_mac == MAC
    assert rows[0].code == EVENT_LISTENER_STREAM_GAP
    assert rows[0].level == "warn"
    assert rows[0].at == at
    assert rows[0].deployment_id == deployment_id_of(reports)


def test_a_redelivered_event_is_not_a_second_timeline_entry(consumer, reports):
    """QoS 1 is at-least-once and an event carries no device-supplied id, so
    identity is (emitter, instant, code). A duplicated row would be a lie about
    how often something happened."""
    message = device_event(reports, at=utcnow())

    assert consumer.consume(message) is ReportOutcome.EVENT
    assert consumer.consume(message) is ReportOutcome.DUPLICATE_EVENT

    with reports() as db:
        assert len(list(db.scalars(select(DeviceEvent)).all())) == 1


def test_an_aggregator_level_event_dedupes_the_same_way(consumer, reports):
    """The `NULLS NOT DISTINCT` case. Without it Postgres treats every event
    with no `listener_mac` as unique and the index would dedupe only Listener
    events — so exactly the lifecycle events an Aggregator emits about itself
    would double on redelivery."""
    message = device_event(reports, code="config_applied", mac=None, at=utcnow())

    assert consumer.consume(message) is ReportOutcome.EVENT
    assert consumer.consume(message) is ReportOutcome.DUPLICATE_EVENT

    with reports() as db:
        rows = list(db.scalars(select(DeviceEvent)).all())
    assert len(rows) == 1
    assert rows[0].listener_mac is None


def test_the_same_code_at_a_different_instant_is_a_different_event(consumer, reports):
    """Dedupe must not swallow a recurring fault. A stream gap every minute is
    a minute-by-minute story, and collapsing it would hide the pattern E7's
    alerts are built to notice."""
    now = utcnow()
    consumer.consume(device_event(reports, at=now))
    consumer.consume(device_event(reports, at=now + timedelta(seconds=60)))

    with reports() as db:
        assert len(list(db.scalars(select(DeviceEvent)).all())) == 2


def test_an_event_from_an_unprovisioned_aggregator_raises_the_alert(consumer, reports):
    """Same membership check as the reported channel (spec 4.3 item 3): an
    event is not a way around provisioning."""
    message = device_event(reports, agg="never-provisioned")

    assert consumer.consume(message) is ReportOutcome.UNPROVISIONED

    with reports() as db:
        assert list(db.scalars(select(DeviceEvent)).all()) == []
    assert len(alerts_of(reports, ALERT_PROVISIONING_REQUIRED)) == 1


# --- The diff helper --------------------------------------------------------


def test_differing_keys_reports_added_removed_and_changed():
    assert differing_keys({"a": 1, "b": 2}, {"a": 1, "b": 3}) == ["b"]
    assert differing_keys({"a": 1, "c": 9}, {"a": 1}) == ["c"]
    assert differing_keys({"a": 1}, {"a": 1, "d": 4}) == ["d"]
    assert differing_keys({"a": 1}, {"a": 1}) == []


def test_differing_keys_is_bounded():
    """It lands in an audit row and on the timeline; a device reporting a
    wholly different config must not write the whole key space there."""
    reported = {f"k.{index}": index for index in range(100)}
    assert len(differing_keys(reported, {})) == 20


# --- ACCEPTANCE-adjacent: the whole path against a real broker --------------


def _provision(url: str, kek: str, out) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--out", str(out), "--host", "127.0.0.1"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": url,
            "EOE_SESSION_SECRET": f"consumer-{uuid.uuid4().hex}",
            "EOE_KEK": kek,
        },
        timeout=180,
    )
    assert result.returncode == 0, f"devbroker failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def live_stack(tmp_path_factory):
    """A provisioned broker plus its own database, for the live test only."""
    out = tmp_path_factory.mktemp("consumer-certs")
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


async def test_a_device_publishing_its_report_drives_the_revision_to_applied(live_stack):
    """The whole path, end to end: a device publishes a reported state with its
    OWN broker credential, the subscription filters this consumer registered
    deliver it, the topic is parsed back into an identity, and the revision
    reaches `applied` — with no test-side shortcut anywhere between.

    This is what proves `filters` and `handle` are wired correctly. Every test
    above hands the consumer a message the suite built itself, so all of them
    would pass with a filter list that subscribed to nothing at all.
    """
    broker, factory, store, manifest = live_stack
    revision_id = make_revision(factory)
    consumer = ReportedConsumer(factory)

    coords = load_broker_coordinates(factory, store)
    deployment_id = next(c.deployment_id for c in coords if c.slug == RC)
    seen: asyncio.Queue[ReportOutcome] = asyncio.Queue()

    async def record(message: InboundMessage) -> None:
        await seen.put(await consumer.handle(message))

    manager = MqttClientManager(lambda: coords)
    manager.subscribe(consumer.filters, record)
    async with manager:
        await manager.wait_connected(deployment_id)

        username = device_username(AGG)
        password = next(a["password"] for a in manifest["accounts"] if a["username"] == username)
        payload = encode(
            ReportedAggregatorState(
                reported_at=datetime.now(UTC),
                applied_revision_id=revision_id,
                config=SNAPSHOT,
                checksum=config_checksum(SNAPSHOT),
            )
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
            payload.decode(),
        )
        assert published.returncode == 0, published.stderr
        outcome = await asyncio.wait_for(seen.get(), timeout=30)

    assert outcome is ReportOutcome.APPLIED
    with factory() as db:
        assert (
            db.scalars(select(ConfigRevision.state).where(ConfigRevision.id == revision_id)).one()
            == RevisionState.APPLIED
        )
        stored = latest_state(db, "aggregator", platform_uuid_of(factory))
        assert stored is not None
        assert stored.config == SNAPSHOT


def test_the_consumer_subscribes_to_every_device_to_platform_topic(reports):
    """The subscription set is the published contract's, not a second list —
    which is what keeps a topic added to spec 7.2 from being delivered to E3.4
    and silently not to E3.5."""
    consumer = ReportedConsumer(reports)
    filters = consumer.filters(RC)
    assert filters == (
        f"eoe/{RC}/agg/+/reported",
        f"eoe/{RC}/agg/+/status",
        f"eoe/{RC}/agg/+/event",
        f"eoe/{RC}/agg/+/lst/+/reported",
    )
    for unwanted in ("desired", "cmd"):
        assert not any(unwanted in topic_filter for topic_filter in filters), (
            "subscribing to a platform-published topic would echo the control plane into itself"
        )
