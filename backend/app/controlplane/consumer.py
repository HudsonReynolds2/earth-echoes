"""The reported consumer (task E3.5, extended by E3.8; spec 6.1, 6.4, 7.3, 7.4, 4.3, 9.3).

The second half of the spec 6.4 loop. E3.4 publishes desired config to a
retained topic and stops; this module listens to everything a device says
back — reported Aggregator state, reported Listener state, events, and the
retained status topic — and turns it into four things: revision transitions
through the E3.6 machine, `device_state` rows (spec 6.1's "last state the
device sent"), `device_event` rows, and `aggregator_status` rows (spec 9.3's
live online verdict).

It is a LIBRARY, like the client manager it sits on. `ReportedConsumer.handle`
satisfies `broker.MessageHandler`, so E3.7's worker wires the two together
with `manager.subscribe(consumer.filters, consumer.handle)`; nothing
constructs it yet.

Five properties are load-bearing, and each is a correctness bug if a later
edit drops it:

1. **Identity comes from the TOPIC, never from a payload.** The broker's
   per-device ACL (spec 7.1) restricts each Aggregator to its own subtree, so
   the `{agg}` and `{mac}` segments of the topic a message arrived on are
   authenticated. None of the spec 7.3 inbound payloads carries an identity
   field, and none should grow one: a payload field is a self-declaration, and
   trusting it would let any device report on behalf of any other.

2. **Identity questions go to the E1.5 services, which never touch
   inventory.** A MAC reported under the wrong Aggregator is quarantined and
   alerted, and the Listener row is left exactly as it was (spec 4.3 item 2,
   "rather than overwriting inventory"). This module does not reimplement any
   of that logic and must never grow a write to an inventory table.

3. **A stale report is dropped whole.** Spec 7.4 tolerates out-of-order
   delivery, and a report older than the one already stored describes a world
   that has moved on: acting on it would drive a healthy device to `drifted`
   on the strength of late news. Strictly older, not older-or-equal — see
   `_is_stale`.

4. **A device that contradicts itself is rejected at the boundary, with no
   transition** (D70). The report carries both `config` and `checksum`; if the
   checksum is not that config's, the message is malformed rather than a
   reconciliation outcome, and an unparseable report is not evidence about
   whether the config was applied.

5. **`transition()` is still the only writer of revision state.** Every state
   change here goes through the E3.6 machine, which is what keeps the spec 6.2
   lifecycle true of every row. States with no legal exit under a report —
   `drifted`, `failed`, `superseded` — are checked for FIRST and left alone,
   rather than offered to the machine and allowed to raise.

The status topic (E3.8) is the one path here that does NOT obey property 3,
and the exception is load-bearing rather than an oversight: a device composes
its will at CONNECT time, so an LWT's `at` predates every `online` that
followed it. Ordering status by the payload clock would reject the single
message that matters most and leave dead devices reading online forever. See
`_status` and `models.AggregatorStatus`.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audit import record_audit
from app.config.canonical import config_checksum
from app.contracts.mqtt import (
    DeviceEvent as DeviceEventPayload,
)
from app.contracts.mqtt import (
    MqttPayload,
    ParsedTopic,
    PayloadError,
    ReportedAggregatorState,
    ReportedListenerState,
    StatusMessage,
    TopicError,
    TopicKind,
    decode,
    deployment_subscriptions,
    describe,
    parse_topic,
)
from app.controlplane.broker import InboundMessage
from app.controlplane.revision_state import (
    RevisionState,
    TransitionRecord,
    Trigger,
    load_for_transition,
    parse_state,
    transition,
)
from app.inventory.identity import (
    IdentityOutcome,
    ProvisioningRequiredError,
    ReportedIdentity,
    handle_reported_identity,
    quarantine_report,
    require_known_aggregator,
)
from app.models import AggregatorStatus, DeviceEvent, DeviceState, Pod

log = logging.getLogger(__name__)

#: The audit action for a revision moved by a device report. Distinct from
#: E3.4's `revision.publish` because the two answer different questions on the
#: E3.11 timeline: who published this, versus what the device said back.
AUDIT_ACTION = "revision.report"

#: The E3.5 quarantine reason for a Listener that reports before anyone
#: entered it in inventory (D76). Not a conflict — E1.5 has no opinion on it
#: by design — so no `duplicate_identity` alert opens.
QUARANTINE_UNKNOWN_MAC = "unknown_mac"

#: How many differing key NAMES a mismatch detail carries. Bounded because the
#: detail lands in an audit row and on the timeline, and a device reporting a
#: wholly different config would otherwise write the entire key space there.
#: Names only, never values: snapshots hold secret markers (rule R2).
MAX_DIFFERING_KEYS = 20


class ReportOutcome(StrEnum):
    """What one delivered message did.

    Returned rather than logged-and-forgotten so the suite can assert on the
    decision instead of on its side effects alone, and so E3.7's worker can
    count outcomes without re-deriving them. Every branch below ends in
    exactly one of these.
    """

    #: pending -> applied. The device reported the revision it was given.
    APPLIED = "applied"
    #: pending -> failed (report_error): a coherent report of the wrong config.
    REJECTED = "rejected"
    #: applied -> drifted. The device diverged from what it had applied.
    DRIFTED = "drifted"
    #: The report was stored and moved no revision — a replay, a device that
    #: has applied nothing yet, or a revision in a state no report can move.
    UNCHANGED = "unchanged"
    #: Late delivery of superseded news (spec 7.4), and ONLY that: the report
    #: predates one already stored, so nothing was stored and nothing moved.
    #: The one-meaning-per-value discipline D70 applies to `Trigger.TIMEOUT`.
    STALE = "stale"
    #: The report went to `quarantined_report` and inventory was not touched.
    QUARANTINED = "quarantined"
    #: The reporter is in no inventory row; a `provisioning_required` alert is
    #: open (spec 4.3 item 3).
    UNPROVISIONED = "unprovisioned"
    #: Undecodable, or a device whose checksum contradicts its own config.
    MALFORMED = "malformed"
    #: A device event was persisted.
    EVENT = "event"
    #: The same event delivered twice (QoS 1 is at-least-once).
    DUPLICATE_EVENT = "duplicate_event"
    #: A known device reporting on a deployment that is not the one it lives
    #: in, or naming a revision that belongs to another device.
    MISROUTED = "misrouted"
    #: An Aggregator called itself online, or its LWT said offline (E3.8).
    #: Both spellings of the spec 9.3 verdict, counted apart because a
    #: fleet going quiet is the shape of an outage.
    ONLINE = "online"
    OFFLINE = "offline"
    #: A topic this consumer does not own — a desired or cmd message the
    #: platform published, echoed back by a hand-rolled subscription.
    NOT_MINE = "not_mine"


@dataclass(frozen=True)
class DeviceRef:
    """One device in the vocabulary `config_revision` and `device_state` share.

    `entity_id` is the PLATFORM UUID for an Aggregator and the MAC for a
    Listener (D75) — resolved from the topic's `aggregator_uuid`, never
    string-copied from it. Spec 4.2 keeps an Aggregator's three identifiers
    distinct, and a report matched against the wrong one finds no revision at
    all and reads as a device that never answered.
    """

    entity_type: str
    entity_id: str
    deployment_id: uuid.UUID


def differing_keys(reported: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    """The key NAMES on which two configs disagree, bounded and sorted.

    Names only. Both sides hold secret markers where a key is secret (spec
    5.4), and device-supplied values are of unknown provenance; the operator
    needs to know WHICH settings disagree, and the values are one click away
    in the revision and the reported state.
    """
    changed = {key for key in reported.keys() & desired.keys() if reported[key] != desired[key]}
    return sorted(changed | (reported.keys() ^ desired.keys()))[:MAX_DIFFERING_KEYS]


class ReportedConsumer:
    """Consumes the device-to-platform topics for every deployment.

    One session per message, committed once at the end: the identity rows, the
    event row, the state row, the revision transition and its audit row seal
    or roll back together, which is the `record_audit` convention every other
    module here follows.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def filters(self, slug: str) -> tuple[str, ...]:
        """The topic filters this consumer wants on one deployment.

        `deployment_subscriptions` verbatim — the whole device-to-platform set,
        including the status topic E3.8 will claim. Subscribing to it now and
        dropping it in `handle` keeps the namespace in one place: a second
        filter list here would be the drift the single contracts module exists
        to prevent.
        """
        return deployment_subscriptions(slug)

    async def handle(self, message: InboundMessage) -> ReportOutcome:
        """`broker.MessageHandler`: one delivered message, start to finish.

        The work is synchronous SQLAlchemy, so it runs in a worker thread
        rather than blocking the event loop that is servicing every other
        deployment's connection — the same treatment `load_broker_coordinates`
        gets. Exceptions propagate: the manager logs them and keeps reading,
        so one device's bad message cannot cost a deployment its control plane.
        """
        return await asyncio.to_thread(self.consume, message)

    def consume(self, message: InboundMessage) -> ReportOutcome:
        """The synchronous body of `handle`, callable directly from tests."""
        try:
            topic = parse_topic(message.topic)
        except TopicError as error:
            # Only reachable if a broker delivered something outside the
            # filters we subscribed to. Loud, because the alternative is
            # guessing which device a malformed topic meant.
            log.warning("dropping a message on an unparseable topic: %s", error)
            return ReportOutcome.MALFORMED

        match topic.kind:
            case TopicKind.AGGREGATOR_REPORTED:
                return self._aggregator_report(message, topic)
            case TopicKind.LISTENER_REPORTED:
                return self._listener_report(message, topic)
            case TopicKind.AGGREGATOR_EVENT:
                return self._event(message, topic)
            case TopicKind.AGGREGATOR_STATUS:
                return self._status(message, topic)
            case _:
                # A desired or cmd topic: the platform's own publishes coming
                # back. `deployment_subscriptions` excludes them precisely so
                # this cannot happen; if it does, something is subscribing by
                # hand and echoing the control plane into itself.
                log.warning("unexpected platform-published topic delivered: %s", message.topic)
                return ReportOutcome.NOT_MINE

    # -- reported Aggregator state ------------------------------------------

    def _aggregator_report(self, message: InboundMessage, topic: ParsedTopic) -> ReportOutcome:
        payload = self._decode(ReportedAggregatorState, message)
        if payload is None:
            return ReportOutcome.MALFORMED

        with self._sessions() as db:
            try:
                aggregator = require_known_aggregator(db, topic.agg)
            except ProvisioningRequiredError:
                # The alert is staged, not raised away: spec 4.3 item 3 wants
                # an unprovisioned reporter surfaced, not silently dropped.
                db.commit()
                log.info("reported state from unprovisioned aggregator %s", topic.agg)
                return ReportOutcome.UNPROVISIONED

            deployment_id = db.scalars(
                select(Pod.deployment_id).where(Pod.id == aggregator.pod_id)
            ).one()
            if deployment_id != message.deployment_id:
                return self._misrouted(topic.agg, deployment_id, message.deployment_id)

            device = DeviceRef("aggregator", str(aggregator.id), deployment_id)
            outcome = self._record_report(
                db,
                device,
                reported_at=payload.reported_at,
                applied_revision_id=payload.applied_revision_id,
                config=payload.config,
                checksum=payload.checksum,
                health=(
                    payload.health.model_dump(mode="json", exclude_none=True)
                    if payload.health is not None
                    else None
                ),
            )
            db.commit()
            return outcome

    # -- reported Listener state --------------------------------------------

    def _listener_report(self, message: InboundMessage, topic: ParsedTopic) -> ReportOutcome:
        payload = self._decode(ReportedListenerState, message)
        if payload is None:
            return ReportOutcome.MALFORMED
        assert topic.mac is not None  # a LISTENER_REPORTED topic always carries one

        # `raw` is preserved verbatim in a quarantine row (the E1.5 contract),
        # so it carries the whole payload — markers included, plaintext never.
        identity = ReportedIdentity(
            mac=topic.mac,
            aggregator_uuid=topic.agg,
            reported_at=payload.reported_at,
            source="mqtt",
            raw={"topic": message.topic, "payload": describe(payload)},
        )
        with self._sessions() as db:
            resolution = handle_reported_identity(db, identity)
            match resolution.outcome:
                case IdentityOutcome.PROVISIONING_REQUIRED:
                    db.commit()
                    log.info("listener report from unprovisioned aggregator %s", topic.agg)
                    return ReportOutcome.UNPROVISIONED
                case IdentityOutcome.MAC_CONFLICT | IdentityOutcome.NAME_CONFLICT:
                    # Property 2: the report is quarantined and the Listener
                    # row is untouched. No state is stored and no revision
                    # moves — a report the platform does not believe must not
                    # become the device's reported configuration.
                    db.commit()
                    log.warning(
                        "quarantined a %s for listener %s reported by %s",
                        resolution.outcome,
                        topic.mac,
                        topic.agg,
                    )
                    return ReportOutcome.QUARANTINED
                case IdentityOutcome.UNKNOWN_MAC:
                    # E1.5 deliberately has no opinion here (this is not a
                    # conflict); the reported channel's answer is D76's.
                    quarantine_report(db, identity, QUARANTINE_UNKNOWN_MAC)
                    db.commit()
                    log.info("quarantined a report from unregistered listener %s", topic.mac)
                    return ReportOutcome.QUARANTINED
                case IdentityOutcome.MATCHED:
                    pass

            listener = resolution.listener
            assert listener is not None  # MATCHED always carries the row
            if listener.deployment_id != message.deployment_id:
                return self._misrouted(topic.mac, listener.deployment_id, message.deployment_id)

            device = DeviceRef("listener", listener.mac, listener.deployment_id)
            outcome = self._record_report(
                db,
                device,
                reported_at=payload.reported_at,
                applied_revision_id=payload.applied_revision_id,
                config=payload.config,
                checksum=payload.checksum,
                # Spec 6.5 liveness is stored by E3.9, which adds the columns.
                health=None,
            )
            db.commit()
            return outcome

    # -- LWT status ----------------------------------------------------------

    def _status(self, message: InboundMessage, topic: ParsedTopic) -> ReportOutcome:
        """The spec 9.3 live verdict for one Aggregator (task E3.8).

        Retained, so the broker replays the current value to the platform on
        every reconnect — which is the property that makes a restarted
        platform learn the fleet's liveness without asking anyone. That replay
        must be a no-op when nothing changed, or every platform restart would
        rewrite the whole fleet's "offline since".

        There is deliberately NO staleness comparison here. See
        `models.AggregatorStatus`: a will is composed at connect time, so an
        LWT's `at` predates every `online` that followed it, and comparing it
        would reject the one message that matters most.
        """
        payload = self._decode(StatusMessage, message)
        if payload is None:
            return ReportOutcome.MALFORMED

        with self._sessions() as db:
            try:
                aggregator = require_known_aggregator(db, topic.agg)
            except ProvisioningRequiredError:
                db.commit()
                log.info("status from unprovisioned aggregator %s", topic.agg)
                return ReportOutcome.UNPROVISIONED

            deployment_id = db.scalars(
                select(Pod.deployment_id).where(Pod.id == aggregator.pod_id)
            ).one()
            if deployment_id != message.deployment_id:
                return self._misrouted(topic.agg, deployment_id, message.deployment_id)

            online = payload.state == "online"
            stored = db.scalars(
                select(AggregatorStatus)
                .where(AggregatorStatus.aggregator_id == aggregator.id)
                .with_for_update()
            ).first()
            now = datetime.now(UTC)
            if stored is None:
                db.add(
                    AggregatorStatus(
                        aggregator_id=aggregator.id,
                        deployment_id=deployment_id,
                        online=online,
                        declared_at=payload.at,
                        changed_at=now,
                    )
                )
                log.info("aggregator %s is %s (first status)", topic.agg, payload.state)
            else:
                stored.deployment_id = deployment_id
                stored.declared_at = payload.at
                stored.received_at = now
                if stored.online != online:
                    stored.online = online
                    stored.changed_at = now
                    log.info("aggregator %s is now %s", topic.agg, payload.state)
            db.commit()
            return ReportOutcome.ONLINE if online else ReportOutcome.OFFLINE

    # -- events --------------------------------------------------------------

    def _event(self, message: InboundMessage, topic: ParsedTopic) -> ReportOutcome:
        payload = self._decode(DeviceEventPayload, message)
        if payload is None:
            return ReportOutcome.MALFORMED

        with self._sessions() as db:
            try:
                aggregator = require_known_aggregator(db, topic.agg)
            except ProvisioningRequiredError:
                db.commit()
                return ReportOutcome.UNPROVISIONED

            deployment_id = db.scalars(
                select(Pod.deployment_id).where(Pod.id == aggregator.pod_id)
            ).one()
            if deployment_id != message.deployment_id:
                return self._misrouted(topic.agg, deployment_id, message.deployment_id)

            already = db.scalars(
                select(DeviceEvent).where(
                    DeviceEvent.deployment_id == deployment_id,
                    DeviceEvent.aggregator_uuid == topic.agg,
                    DeviceEvent.listener_mac.is_not_distinct_from(payload.listener_mac),
                    DeviceEvent.at == payload.at,
                    DeviceEvent.code == payload.code,
                )
            ).first()
            if already is not None:
                # QoS 1 is at-least-once. `uq_device_event_delivery` backstops
                # this check against a race, the way E1.5's partial index
                # backstops its alert dedupe.
                return ReportOutcome.DUPLICATE_EVENT

            db.add(
                DeviceEvent(
                    deployment_id=deployment_id,
                    aggregator_uuid=topic.agg,
                    listener_mac=payload.listener_mac,
                    at=payload.at,
                    level=payload.level,
                    code=payload.code,
                    detail=payload.detail,
                )
            )
            db.commit()
            log.info("device event %s from %s (%s)", payload.code, topic.agg, payload.level)
            return ReportOutcome.EVENT

    # -- the shared reported-state path --------------------------------------

    def _record_report(
        self,
        db: Session,
        device: DeviceRef,
        *,
        reported_at: datetime,
        applied_revision_id: uuid.UUID | None,
        config: dict[str, Any],
        checksum: str,
        health: dict[str, Any] | None,
    ) -> ReportOutcome:
        """Store one report and advance its revision. Stages; caller commits."""
        stored = db.scalars(
            select(DeviceState)
            .where(
                DeviceState.entity_type == device.entity_type,
                DeviceState.entity_id == device.entity_id,
            )
            .with_for_update()
        ).first()
        if stored is not None and _is_stale(reported_at, stored.reported_at):
            log.info(
                "ignoring a stale report for %s %s (%s is older than the stored %s)",
                device.entity_type,
                device.entity_id,
                reported_at.isoformat(),
                stored.reported_at.isoformat(),
            )
            return ReportOutcome.STALE

        outcome = self._advance_revision(
            db,
            device,
            applied_revision_id=applied_revision_id,
            config=config,
            checksum=checksum,
            reported_at=reported_at,
        )
        if outcome is ReportOutcome.MISROUTED:
            # A report naming another device's revision says nothing reliable
            # about this one; storing it as this device's state would launder
            # the confusion into the record.
            return outcome

        if stored is None:
            stored = DeviceState(entity_type=device.entity_type, entity_id=device.entity_id)
            db.add(stored)
        stored.deployment_id = device.deployment_id
        stored.reported_at = reported_at
        stored.applied_revision_id = applied_revision_id
        stored.checksum = checksum
        stored.config = config
        stored.health = health
        return outcome

    def _advance_revision(
        self,
        db: Session,
        device: DeviceRef,
        *,
        applied_revision_id: uuid.UUID | None,
        config: dict[str, Any],
        checksum: str,
        reported_at: datetime,
    ) -> ReportOutcome:
        """The spec 6.2 half of a report: which edge, if any, this one drives."""
        if applied_revision_id is None:
            # A device that has applied nothing still reports its state (D67).
            return ReportOutcome.UNCHANGED

        revision = load_for_transition(db, applied_revision_id)
        if revision is None:
            # Un-FK'd by design (D33): a revision can be pruned while a device
            # still names it. Worth storing the state, worth no transition.
            log.info(
                "%s %s reported revision %s, which is not in the revision history",
                device.entity_type,
                device.entity_id,
                applied_revision_id,
            )
            return ReportOutcome.UNCHANGED
        if (revision.target_type, revision.target_id) != (device.entity_type, device.entity_id):
            log.warning(
                "%s %s reported revision %s, which belongs to %s %s",
                device.entity_type,
                device.entity_id,
                applied_revision_id,
                revision.target_type,
                revision.target_id,
            )
            return ReportOutcome.MISROUTED

        state = parse_state(revision.state)
        matches = checksum == revision.checksum
        # Checked BEFORE offering anything to the machine (property 5): from
        # these four states spec 6.2 has no edge a device report can drive.
        # `drifted` and `failed` are waiting on an operator rather than on
        # news; `draft` was never published, so a device cannot have applied
        # it; and `superseded` is spec 7.4's "ignoring stale reports by
        # comparing revision timestamps", already decided by the publish that
        # closed this revision. The report is still STORED in every case — it
        # is current news that the device has not caught up yet, which is
        # exactly what E3.7's drift sweep needs to read.
        if state in (
            RevisionState.DRAFT,
            RevisionState.DRIFTED,
            RevisionState.FAILED,
            RevisionState.SUPERSEDED,
        ):
            log.info(
                "%s %s reported revision %s, which is %s: no spec 6.2 edge to take",
                device.entity_type,
                device.entity_id,
                applied_revision_id,
                state,
            )
            return ReportOutcome.UNCHANGED

        if state is RevisionState.PENDING and matches:
            record = transition(db, revision, RevisionState.APPLIED, Trigger.REPORT_MATCH)
            self._audit(db, record, device, reported_at=reported_at, checksum=checksum)
            return ReportOutcome.APPLIED
        if state is RevisionState.PENDING:
            # D70: a coherent report of config that is not this revision's is a
            # DEFINITE negative answer, so it fails now rather than waiting out
            # the window — which would report a timeout for a device that
            # answered in two seconds, and make `failed(timeout)` cover two
            # stories that call for opposite responses.
            record = transition(
                db,
                revision,
                RevisionState.FAILED,
                Trigger.REPORT_ERROR,
                detail={"differing_keys": differing_keys(config, revision.snapshot)},
            )
            self._audit(db, record, device, reported_at=reported_at, checksum=checksum)
            return ReportOutcome.REJECTED
        if state is RevisionState.APPLIED and not matches:
            record = transition(
                db,
                revision,
                RevisionState.DRIFTED,
                Trigger.REPORT_DIVERGED,
                detail={"differing_keys": differing_keys(config, revision.snapshot)},
            )
            self._audit(db, record, device, reported_at=reported_at, checksum=checksum)
            return ReportOutcome.DRIFTED
        # `applied` and matching: the idempotent path. A replayed report is
        # spec 7.4's "idempotent by applied_revision_id plus checksum" — same
        # revision, same checksum, so there is nothing left to say.
        return ReportOutcome.UNCHANGED

    # -- shared helpers ------------------------------------------------------

    def _decode[PayloadT: MqttPayload](
        self, model: type[PayloadT], message: InboundMessage
    ) -> PayloadT | None:
        """Parse one payload, or None with the reason logged.

        Two boundary refusals live together here because they mean the same
        thing — the message is malformed, not a reconciliation outcome, so it
        drives no state (D70). `PayloadError` is safe to log verbatim: it names
        the fields that failed and never their values (D68).
        """
        try:
            payload = decode(model, message.payload)
        except PayloadError as error:
            log.warning("dropping a message on %s: %s", message.topic, error)
            return None
        checksum = getattr(payload, "checksum", None)
        config = getattr(payload, "config", None)
        if checksum is not None and config is not None and checksum != config_checksum(config):
            # The device contradicts ITSELF. Left for the timeout on purpose:
            # an unparseable report is not evidence about whether the config
            # was applied, so `failed(timeout)` stays true if it comes to that.
            log.warning(
                "%s reported a checksum that is not its own config's; "
                "check the firmware's implementation of the checksum recipe",
                message.topic,
            )
            return None
        return payload

    def _misrouted(self, who: str, home: uuid.UUID, arrived_on: uuid.UUID) -> ReportOutcome:
        """A device reporting on a deployment that is not the one it lives in.

        The broker ACL should make this impossible, which is why it is a
        warning and not a quarantine: it means a device holds a credential for
        a namespace it does not belong to, and the fix is on the broker, not
        in inventory.
        """
        log.warning(
            "%s reported on deployment %s but lives in %s; dropping",
            who,
            arrived_on,
            home,
        )
        return ReportOutcome.MISROUTED

    def _audit(
        self,
        db: Session,
        record: TransitionRecord,
        device: DeviceRef,
        *,
        reported_at: datetime,
        checksum: str,
    ) -> None:
        """One audit row per state-moving report, and none otherwise.

        Reports arrive continuously; a row per delivery would bury the
        transitions that matter under heartbeat noise, which is D72's reasoning
        for the idempotent republish and holds the same way here. The `device_state`
        row and the `device_event` rows are the record of what was said; the
        audit trail is the record of what CHANGED.
        """
        record_audit(
            db,
            action=AUDIT_ACTION,
            entity_type="config_revision",
            entity_id=str(record.revision_id),
            actor_user_id=None,  # system-originated: a device said so, not a user
            scope=device.deployment_id,
            detail={
                "target_type": device.entity_type,
                "target_id": device.entity_id,
                "from_state": record.source.value,
                "to_state": record.target_state.value,
                "trigger": record.trigger.value,
                "reported_at": reported_at.isoformat(),
                "reported_checksum": checksum,
                **(record.detail or {}),
            },
        )


def _is_stale(incoming: datetime, stored: datetime) -> bool:
    """Whether a report is late delivery of superseded news (spec 7.4).

    STRICTLY older, deliberately. An equal timestamp is a redelivery of the
    message already processed, and letting it through the full comparison is
    what makes idempotency a property of `applied_revision_id` plus checksum —
    the way spec 7.4 words it — rather than of a timestamp shortcut that would
    hide a broken comparison behind an early return.
    """
    return incoming < stored


def latest_state(db: Session, entity_type: str, entity_id: str) -> DeviceState | None:
    """One device's last reported state, or None if it has never reported.

    Exported for E3.7's drift re-compare and E3.11's timeline, so neither has
    to know the `(entity_type, entity_id)` convention this table shares with
    `config_revision`.
    """
    return db.scalars(
        select(DeviceState).where(
            DeviceState.entity_type == entity_type, DeviceState.entity_id == entity_id
        )
    ).first()


def delete_device_state_for(db: Session, entity_type: str, entity_id: str) -> None:
    """Drop a device's reported state when the device is deleted (E1 endpoints).

    The `delete_overrides_for` precedent. `device_state` is CURRENT state, not
    evidence: a Listener re-added under a MAC that once belonged to another
    physical device would otherwise inherit its predecessor's reported config
    and read as reconciled before it had said a word. `device_event` rows are
    evidence and deliberately survive (D33).
    """
    stored = latest_state(db, entity_type, entity_id)
    if stored is not None:
        db.delete(stored)
