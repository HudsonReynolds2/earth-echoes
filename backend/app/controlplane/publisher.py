"""The desired-config publish path (task E3.4; spec 6.2, 6.4, 7.2, 7.3).

`publish_revision` is the ONLY way a `config_revision` reaches a device. It is
the first half of the spec 6.4 loop — steps 1 and 2, "publish on change" — and
E3.5 is the second half. E2 built the revisions and stopped at `draft` (D55);
this is the call-through E2.6 left a flag for, wired at E3.13.

Four properties are load-bearing here, and each one is a data-loss or
lifecycle bug if a later edit drops it:

1. **The snapshot is copied through untouched.** `config_revision.snapshot` IS
   the publishable body (D52): the checksum stored on the row is the checksum
   of exactly these bytes, so a device that echoes what it applied matches by
   construction. Anything that rewrites, reorders or enriches the snapshot on
   the way out breaks that match for every device at once, and the failure
   shows up as fleet-wide phantom drift rather than as an error here.

2. **Retained, always.** Spec 7.2 marks the desired topics retained and spec
   6.4 relies on it: an Aggregator that reconnects after an outage gets its
   desired config from the broker with no polling and no platform involvement.
   Publishing this unretained would look identical in a test that subscribes
   first, which is why the acceptance test subscribes *afterwards*.

3. **Only the newest revision for a device may be published.** This is the
   other half of a pair `revision_state.supersede_open_revisions` documents:
   that function supersedes every other open revision unconditionally, which
   is only safe because publishing an older one is refused here. Removing
   either rule alone silently discards a newer draft an operator was still
   working on.

4. **The publish happens INSIDE the database transaction.** The state change
   is staged, the bytes go to the broker, and only a successful publish is
   committed. A broker outage therefore leaves the revision in `draft` with
   nothing published (`BrokerUnavailable`, as `broker.py` promises), rather
   than a `pending` revision no device was ever told about — which would
   resolve as a spec 6.2 timeout and blame the device for the platform's
   failure. The row lock is held across that publish on purpose: it serializes
   two operators publishing the same revision at once.

The trigger is chosen from the revision's CURRENT state rather than fixed,
because spec 6.2 reaches `pending` by three different edges and they are three
different events on the timeline: `draft` is a first publish, `drifted` is an
operator re-publishing over drift, and `failed` is an operator retrying.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audit import record_audit
from app.contracts.mqtt import (
    QOS,
    DesiredConfig,
    DesiredTarget,
    desired_topic,
    encode,
    listener_desired_topic,
)
from app.controlplane.broker import BrokerUnavailable
from app.controlplane.revision_state import (
    RevisionState,
    TransitionRecord,
    Trigger,
    load_for_transition,
    parse_state,
    supersede_open_revisions,
    transition,
)
from app.models import Aggregator, ConfigRevision, Deployment, Listener, Pod, utcnow

log = logging.getLogger(__name__)

#: The audit action for a publish that moved the revision. Read by the E3.11
#: timeline and by the E0 audit surface.
AUDIT_ACTION = "revision.publish"

#: How a revision's current state decides the spec 6.2 trigger for reaching
#: `pending`. Absent states are handled explicitly below and deliberately do
#: NOT default: a state that grew a new publish edge must be thought about
#: here rather than inheriting `PUBLISH` and mislabelling the timeline.
_TRIGGER_FOR: dict[RevisionState, Trigger] = {
    RevisionState.DRAFT: Trigger.PUBLISH,
    RevisionState.DRIFTED: Trigger.REPUBLISH,
    RevisionState.FAILED: Trigger.RETRY,
}

#: States where this revision is ALREADY the device's desired config. A second
#: publish re-sends the identical retained bytes and changes no state — see
#: `publish_revision` on why re-sending rather than doing nothing.
_ALREADY_DESIRED = frozenset({RevisionState.PENDING, RevisionState.APPLIED})


class PublishError(Exception):
    """Base for every refusal in this module.

    A plain exception rather than the API's `AppError`, following
    `revision_state.IllegalTransition` and `ContractError`: the reconciliation
    worker and the E3.13 apply path both call this from outside the HTTP
    layer, and neither should have an error envelope imposed on it. The API
    boundary translates.
    """


class PublishDisabled(PublishError):
    """`EOE_PUBLISH_ENABLED` is off, so nothing may reach a device."""


class UnknownRevision(PublishError):
    """No `config_revision` with that id."""


class UnknownPublishTarget(PublishError):
    """The revision's device is not in inventory any more.

    Expected, not a bug: `config_revision.target_id` is deliberately un-FK'd
    (D33) so revision history outlives the devices it describes. A revision
    for a decommissioned Aggregator is evidence worth keeping and has no topic
    to go to, which is exactly this.
    """


class StaleRevision(PublishError):
    """This revision is not the newest for its device, or it is terminal.

    Refusing is what makes `supersede_open_revisions` safe (property 3 in the
    module docstring). Publishing a superseded revision would also re-arm
    config an operator has already replaced.
    """


class DesiredPublisher(Protocol):
    """The half of `MqttClientManager` this module needs.

    Structural, so `MqttClientManager` satisfies it without importing anything
    from here, and so a test can record publishes without a live broker while
    the acceptance test uses the real client against a real one.
    """

    async def publish(
        self,
        deployment_id: uuid.UUID,
        topic: str,
        payload: bytes,
        *,
        qos: int = QOS,
        retain: bool = False,
    ) -> None: ...


@dataclass(frozen=True)
class DesiredRoute:
    """Where one revision's payload goes, resolved from LIVE inventory.

    The deployment comes from the Aggregator's pod for both target types —
    including Listener revisions, whose topic hangs off their Aggregator's
    subtree (spec 7.2) and must therefore name the deployment whose broker ACL
    grants that subtree. `config_revision.deployment_id` is historical evidence
    of where the device lived when the revision was cut and is deliberately not
    used for addressing; a device that moved deployments publishes to its
    current home or not at all.

    **`device_id` is not `config_revision.target_id`, and for an Aggregator it
    cannot be.** Spec 4.2 keeps an Aggregator's three identifiers distinct and
    E1 never conflates them: `target_id` carries the PLATFORM UUID
    (`aggregator.id`, the row's primary key), while the `{agg}` topic segment
    and the spec 7.3 `target.id` field are the `aggregator_uuid` — the name the
    device knows itself by and the only one that means anything on the wire
    (D36, D75). Resolving one from the other is this function's job. Listeners
    have one identifier, the MAC, so both are the same string there.
    """

    deployment_id: uuid.UUID
    slug: str
    aggregator_uuid: str
    #: What the DEVICE calls itself: `aggregator_uuid`, or a Listener's MAC.
    device_id: str
    topic: str


@dataclass(frozen=True)
class PublishOutcome:
    """What one `publish_revision` call did, for the caller's response and log.

    `transitioned` is False on the idempotent re-publish path: the bytes went
    out again, the state did not move, and no audit row was written. Callers
    that report "published" to an operator should say so from `state`, which
    is true either way.
    """

    revision_id: uuid.UUID
    topic: str
    deployment_id: uuid.UUID
    checksum: str
    state: RevisionState
    trigger: Trigger | None = None
    transitioned: bool = False
    superseded: tuple[uuid.UUID, ...] = field(default_factory=tuple)


def resolve_desired_route(db: Session, revision: ConfigRevision) -> DesiredRoute:
    """The spec 7.2 desired topic for a revision's target, or raise.

    Every identifier passes through the E3.3 topic builders, which validate
    rather than trust: a slug or MAC that reached a topic string unchecked
    could carry a wildcard and hand one device another's subtree.
    """
    if revision.target_type == "aggregator":
        # `target_id` is the PLATFORM UUID (`aggregator.id`), which is what E2
        # writes; the topic segment is the `aggregator_uuid`. Looking up by the
        # wrong one of spec 4.2's three identifiers finds nothing at all, which
        # is why this is a lookup rather than a string copy (D75).
        try:
            platform_uuid = uuid.UUID(revision.target_id)
        except ValueError as error:
            raise UnknownPublishTarget(
                f"revision {revision.id} targets aggregator {revision.target_id!r}, "
                "which is not a platform UUID; aggregator revisions carry aggregator.id"
            ) from error
        aggregator_row = db.execute(
            select(Aggregator.aggregator_uuid, Deployment.id, Deployment.slug)
            .join(Pod, Pod.id == Aggregator.pod_id)
            .join(Deployment, Deployment.id == Pod.deployment_id)
            .where(Aggregator.id == platform_uuid)
        ).first()
        if aggregator_row is None:
            raise UnknownPublishTarget(
                f"revision {revision.id} targets aggregator {revision.target_id!r}, "
                "which is not in inventory"
            )
        aggregator_uuid, deployment_id, slug = aggregator_row
        return DesiredRoute(
            deployment_id=deployment_id,
            slug=slug,
            aggregator_uuid=aggregator_uuid,
            device_id=aggregator_uuid,
            topic=desired_topic(slug, aggregator_uuid),
        )

    if revision.target_type == "listener":
        listener_row = db.execute(
            select(Listener.mac, Aggregator.aggregator_uuid, Deployment.id, Deployment.slug)
            .join(Aggregator, Aggregator.id == Listener.aggregator_id)
            .join(Pod, Pod.id == Aggregator.pod_id)
            .join(Deployment, Deployment.id == Pod.deployment_id)
            .where(Listener.mac == revision.target_id)
        ).first()
        if listener_row is None:
            raise UnknownPublishTarget(
                f"revision {revision.id} targets listener {revision.target_id!r}, "
                "which is not in inventory"
            )
        mac, aggregator_uuid, deployment_id, slug = listener_row
        return DesiredRoute(
            deployment_id=deployment_id,
            slug=slug,
            aggregator_uuid=aggregator_uuid,
            # A Listener has exactly one identifier, so `target_id`, the topic
            # segment and the payload's `target.id` are all the same MAC.
            device_id=mac,
            topic=listener_desired_topic(slug, aggregator_uuid, mac),
        )

    # Unreachable through the table's CHECK constraint; loud anyway, because
    # the alternative to a raise here is publishing to a topic built from a
    # target type nobody has thought about.
    raise UnknownPublishTarget(
        f"revision {revision.id} has target_type {revision.target_type!r}, "
        "which has no spec 7.2 desired topic"
    )


def desired_payload(revision: ConfigRevision, route: DesiredRoute) -> DesiredConfig:
    """The spec 7.3 body for one revision.

    `config` is `revision.snapshot` and `checksum` is `revision.checksum`, both
    verbatim — see property 1 in the module docstring. The snapshot already
    carries secret MARKERS rather than plaintext (spec 5.4, 8), because E2 put
    them there; this module has no secret-handling of its own and must never
    grow any.

    `target.id` is `route.device_id`, NOT `revision.target_id`: an Aggregator's
    row primary key means nothing to the Aggregator, which knows itself by its
    `aggregator_uuid` (D75).
    """
    return DesiredConfig(
        revision_id=revision.id,
        generated_at=utcnow(),
        target=DesiredTarget(type=revision.target_type, id=route.device_id),  # type: ignore[arg-type]
        config=revision.snapshot,
        checksum=revision.checksum,
    )


def newest_revision_for_target(db: Session, revision: ConfigRevision) -> ConfigRevision | None:
    """The latest revision for this revision's device, by (created_at, id).

    The same total order `open_revisions_for_target` sorts by, so "newest" and
    "oldest first" cannot disagree. `created_at` alone would not be a total
    order: it defaults to `now()`, which Postgres holds constant for a whole
    transaction, so two revisions written together share it exactly.
    """
    return db.scalars(
        select(ConfigRevision)
        .where(
            ConfigRevision.target_type == revision.target_type,
            ConfigRevision.target_id == revision.target_id,
        )
        .order_by(ConfigRevision.created_at.desc(), ConfigRevision.id.desc())
        .limit(1)
    ).first()


async def publish_revision(
    session_factory: sessionmaker[Session],
    publisher: DesiredPublisher,
    revision_id: uuid.UUID,
    *,
    publish_enabled: bool,
    actor_user_id: uuid.UUID | None = None,
) -> PublishOutcome:
    """Publish one revision's desired config, retained, and advance its state.

    Owns its own transaction rather than joining the caller's: a revision can
    only be published after it is committed, so E2's apply commits first
    (E3.13) and calls this second.

    `publish_enabled` is required and unnamed-argument-proof on purpose. The
    refusal lives here rather than at each call site so that no future caller
    can reach a device by forgetting the flag; the VALUE still comes from
    `Settings.publish_enabled` (`EOE_PUBLISH_ENABLED`, D61, default on at
    E3.13), because settings do not belong in the control-plane core.

    Re-publishing a revision that is already `pending` or `applied` re-sends
    the identical retained bytes and returns with `transitioned=False`. It
    re-sends rather than returning early because that is the operator's repair
    for a broker that lost its retained store — the bytes are byte-identical
    and idempotent by construction, so a device that already holds them sees a
    matching checksum and nothing happens.
    """
    if not publish_enabled:
        raise PublishDisabled(
            f"EOE_PUBLISH_ENABLED is off; refusing to publish revision {revision_id} "
            "(the flag exists so nothing reaches a device before E3.13 turns it on)"
        )

    with session_factory() as db:
        try:
            revision = load_for_transition(db, revision_id)
            if revision is None:
                raise UnknownRevision(f"no config_revision {revision_id}")

            state = parse_state(revision.state)
            if state is RevisionState.SUPERSEDED:
                raise StaleRevision(
                    f"revision {revision_id} is superseded; a newer revision is the "
                    "device's desired config and re-arming this one would undo it"
                )
            newest = newest_revision_for_target(db, revision)
            if newest is not None and newest.id != revision.id:
                raise StaleRevision(
                    f"revision {revision_id} is not the newest for "
                    f"{revision.target_type} {revision.target_id!r} ({newest.id} is); "
                    "publishing it would supersede the newer one"
                )

            route = resolve_desired_route(db, revision)
            payload = encode(desired_payload(revision, route))

            record: TransitionRecord | None = None
            superseded: list[TransitionRecord] = []
            if state not in _ALREADY_DESIRED:
                trigger = _TRIGGER_FOR[state]
                record = transition(
                    db, revision, RevisionState.PENDING, trigger, actor_user_id=actor_user_id
                )
                superseded = supersede_open_revisions(db, revision, actor_user_id=actor_user_id)
                record_audit(
                    db,
                    action=AUDIT_ACTION,
                    entity_type="config_revision",
                    entity_id=str(revision.id),
                    actor_user_id=actor_user_id,
                    scope=route.deployment_id,
                    detail={
                        "target_type": revision.target_type,
                        "target_id": revision.target_id,
                        "topic": route.topic,
                        "checksum": revision.checksum,
                        "from_state": record.source.value,
                        "to_state": record.target_state.value,
                        "trigger": record.trigger.value,
                        "superseded": [str(item.revision_id) for item in superseded],
                    },
                )

            # Read off the row BEFORE the commit expires it: the outcome must
            # outlive this session, and re-reading it afterwards would either
            # raise (detached) or cost a second query to say what we already
            # know.
            outcome = PublishOutcome(
                revision_id=revision.id,
                topic=route.topic,
                deployment_id=route.deployment_id,
                checksum=revision.checksum,
                state=RevisionState.PENDING if record is not None else state,
                trigger=record.trigger if record is not None else None,
                transitioned=record is not None,
                superseded=tuple(item.revision_id for item in superseded),
            )
            # Last, and inside the transaction: nothing is committed until the
            # broker has taken the message (property 4).
            await publisher.publish(route.deployment_id, route.topic, payload, qos=QOS, retain=True)
            db.commit()
        except Exception:
            db.rollback()
            raise

        log.info(
            "published revision %s to %s (%s, %d superseded)",
            outcome.revision_id,
            outcome.topic,
            outcome.trigger.value if outcome.trigger else "re-publish, no transition",
            len(outcome.superseded),
        )
        return outcome


async def publish_all(
    session_factory: sessionmaker[Session],
    publisher: DesiredPublisher | None,
    revisions: Sequence[ConfigRevision],
    *,
    publish_enabled: bool,
    actor_user_id: uuid.UUID | None = None,
) -> set[uuid.UUID]:
    """Publish a batch of freshly created revisions; return the ids that went out.

    **Extracted from `api/config.py::_publish_applied` by E5.7a**, because E5's
    services save is the second caller and two divergent publish loops with two
    error-swallowing policies is a bug that surfaces months later as "some
    applies publish and some do not". Behaviour is unchanged from E3.13's:

    * Called AFTER the caller's commit, so nothing here can undo an operator's
      edit. A broker that is down costs a publish and not the work; that
      revision stays `draft`, is reported as such, and
      `POST /revisions/{id}/publish` retries it — the same route drift repair
      uses (D82), so there is one path and one set of refusals.
    * **One failure does not abort the rest.** A fleet-wide apply can span
      deployments whose brokers are independent, and failing the whole call
      over one unreachable broker would leave the operator no way to tell which
      devices were told.
    * `publisher` is None when `EOE_PUBLISH_ENABLED` is off, and also in any
      process that never ran the lifespan (D86) — a test client used without
      its context manager, for one. Both mean "nothing may reach a device",
      which is exactly `draft`.
    """
    if publisher is None or not publish_enabled or not revisions:
        return set()

    published: set[uuid.UUID] = set()
    for revision in revisions:
        try:
            await publish_revision(
                session_factory,
                publisher,
                revision.id,
                publish_enabled=True,
                actor_user_id=actor_user_id,
            )
        except (PublishError, BrokerUnavailable) as error:
            # Logged, counted, and left as draft. Raising would abort the rest
            # of a fleet-wide apply over one unreachable deployment.
            log.warning("could not publish revision %s: %s", revision.id, error)
            continue
        published.add(revision.id)
    return published
