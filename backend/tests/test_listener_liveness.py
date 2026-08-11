"""Gate 47: Listener liveness persistence (task E3.9; spec 6.5, 7.3, 9.3).

The phase document's three acceptance criteria, each named below:

* `test_a_sleeping_listener_reads_as_healthy` — spec 9.3's rule that
  `sleeping` displays as healthy, which is the entire point of spec 6.5 and
  the easiest thing here to get wrong.
* `test_a_missed_wake_event_flips_the_listener_offline`
* `test_the_liveness_journey_lands_on_the_timeline` — the transitions and the
  events survive as durable rows for E3.11 to render.

The property running underneath all of them: **the platform never computes a
wake window.** The Listener declares its wake time over the local HaLow link,
the Aggregator trusts that declaration and applies
`listener.wake_grace_seconds` itself, and the platform records the outcome.
`test_the_platform_never_computes_a_wake_window` is the guard — an expired
`expected_wake_at` with no event behind it changes nothing, because deciding
otherwise would be the platform second-guessing the only clock that matters.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ephemeral_postgres
from sqlalchemy import delete, select

from app.config.canonical import config_checksum
from app.contracts.mqtt import (
    EVENT_LISTENER_MISSED_WAKE_WINDOW,
    EVENT_LISTENER_STREAM_GAP,
    ListenerLiveness,
    ReportedListenerState,
    encode,
    event_topic,
    listener_reported_topic,
)
from app.contracts.mqtt import DeviceEvent as DeviceEventPayload
from app.controlplane.broker import InboundMessage
from app.controlplane.consumer import ReportedConsumer, ReportOutcome
from app.controlplane.liveness import HEALTHY_LIVENESS, LIVENESS_STATES, listener_verdict
from app.db import create_session_factory
from app.models import (
    ConfigRevision,
    Deployment,
    DeviceEvent,
    DeviceState,
    InventoryAlert,
    QuarantinedReport,
)
from app.seed import seed_demo_hierarchy

RC = "redwood-coast"
AGG = "demo-agg-rc-01"
MAC = "02:EE:0E:01:01:01"

CONFIG = {"capture.mode": "duty_cycle", "audio.sample_rate_hz": 48000}

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
def live(database):
    yield database
    with database() as db:
        for table in (ConfigRevision, DeviceState, DeviceEvent, QuarantinedReport, InventoryAlert):
            db.execute(delete(table))
        db.commit()


@pytest.fixture
def consumer(live) -> ReportedConsumer:
    return ReportedConsumer(live)


def deployment_id_of(factory) -> uuid.UUID:
    with factory() as db:
        return db.scalars(select(Deployment.id).where(Deployment.slug == RC)).one()


def state_of(factory, mac: str = MAC) -> DeviceState | None:
    with factory() as db:
        return db.scalars(
            select(DeviceState).where(
                DeviceState.entity_type == "listener", DeviceState.entity_id == mac
            )
        ).first()


def inbound(factory, topic: str, payload: bytes) -> InboundMessage:
    return InboundMessage(
        deployment_id=deployment_id_of(factory),
        deployment_slug=RC,
        topic=topic,
        payload=payload,
        qos=1,
        retain=False,
    )


def report(
    factory,
    consumer: ReportedConsumer,
    *,
    state: str,
    expected_wake_at: datetime | None = None,
    last_audio_at: datetime | None = None,
    reported_at: datetime | None = None,
) -> ReportOutcome:
    """One `lst/{mac}/reported` publish carrying a spec 6.5 liveness block."""
    payload = ReportedListenerState(
        reported_at=reported_at or datetime.now(UTC),
        applied_revision_id=None,
        config=CONFIG,
        checksum=config_checksum(CONFIG),
        liveness=ListenerLiveness(
            state=state, expected_wake_at=expected_wake_at, last_audio_at=last_audio_at
        ),
    )
    return consumer.consume(
        inbound(factory, listener_reported_topic(RC, AGG, MAC), encode(payload))
    )


def raise_event(
    factory,
    consumer: ReportedConsumer,
    *,
    code: str,
    at: datetime | None = None,
    mac: str | None = MAC,
) -> ReportOutcome:
    payload = DeviceEventPayload(
        at=at or datetime.now(UTC),
        level="warn",
        code=code,
        detail="raised by the aggregator, which is the only thing that may raise it",
        listener_mac=mac,
    )
    return consumer.consume(inbound(factory, event_topic(RC, AGG), encode(payload)))


# --- The verdict (spec 9.3) -------------------------------------------------


@pytest.mark.parametrize("state", LIVENESS_STATES)
def test_the_vocabulary_matches_the_spec_and_nothing_else(state):
    assert state in {"streaming", "sleeping", "offline"}
    assert set(LIVENESS_STATES) == {"streaming", "sleeping", "offline"}


def test_a_sleeping_listener_reads_as_healthy(consumer, live):
    """ACCEPTANCE (phase doc E3.9), and the rule this whole task exists for.

    A duty-cycled Listener is silent for most of its life BY DESIGN. Spec 9.3
    displays `sleeping` as healthy for exactly that reason: a platform that
    painted expected silence as failure would report a correctly-working
    deployment as a fleet-wide outage every single night.
    """
    wake = datetime.now(UTC) + timedelta(minutes=5)
    assert report(live, consumer, state="sleeping", expected_wake_at=wake) is (
        ReportOutcome.UNCHANGED
    )

    row = state_of(live)
    assert row is not None
    assert row.liveness_state == "sleeping"
    assert row.expected_wake_at == wake
    assert listener_verdict(row.liveness_state) == "healthy"
    assert "sleeping" in HEALTHY_LIVENESS


def test_streaming_is_healthy_and_offline_is_not():
    assert listener_verdict("streaming") == "healthy"
    assert listener_verdict("sleeping") == "healthy"
    assert listener_verdict("offline") == "offline"


def test_a_listener_nobody_has_reported_on_is_unknown_not_offline():
    """E1 lets an operator enter a Listener into inventory months before it
    ships. Painting it offline would accuse a device that may not be powered
    on yet of being broken, and would make a fresh deployment look failed."""
    assert listener_verdict(None) == "unknown"


# --- What the report stores -------------------------------------------------


def test_streaming_clears_the_wake_time_it_had_while_asleep(consumer, live):
    """A stale `expected_wake_at` surviving the wake is a value something
    downstream will eventually compare a clock against."""
    report(
        live, consumer, state="sleeping", expected_wake_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    assert state_of(live).expected_wake_at is not None

    report(live, consumer, state="streaming", last_audio_at=datetime.now(UTC))

    row = state_of(live)
    assert row.liveness_state == "streaming"
    assert row.expected_wake_at is None
    assert row.last_audio_at is not None


def test_re_reporting_the_same_state_does_not_restart_the_clock(consumer, live):
    """`liveness_changed_at` is how long it has been in this state. A Listener
    re-reporting `sleeping` every minute must not keep resetting it — the same
    rule the LWT verdict follows for `aggregator_status.changed_at` (D88)."""
    wake = datetime.now(UTC) + timedelta(minutes=5)
    report(live, consumer, state="sleeping", expected_wake_at=wake)
    first = state_of(live).liveness_changed_at

    report(live, consumer, state="sleeping", expected_wake_at=wake)

    assert state_of(live).liveness_changed_at == first


def test_a_real_change_moves_the_clock(consumer, live):
    report(live, consumer, state="streaming")
    first = state_of(live).liveness_changed_at

    report(
        live, consumer, state="sleeping", expected_wake_at=datetime.now(UTC) + timedelta(minutes=5)
    )

    assert state_of(live).liveness_changed_at > first


def test_an_aggregator_report_never_carries_liveness(consumer, live):
    """Aggregators have no spec 6.5 liveness — theirs is the E3.8 LWT verdict.
    A liveness value on an aggregator row would be a second, quieter answer to
    a question that already has an authoritative one, and the
    `liveness_is_listener_only` CHECK refuses it at the database."""
    with live() as db:
        rows = db.scalars(select(DeviceState).where(DeviceState.entity_type == "aggregator")).all()
        assert all(row.liveness_state is None for row in rows)


# --- Missed wake windows ----------------------------------------------------


def test_a_missed_wake_event_flips_the_listener_offline(consumer, live):
    """ACCEPTANCE (phase doc E3.9).

    Spec 6.5: the Aggregator compares its own clock to the time the Listener
    promised, adds the grace period, and raises this event. The platform
    records the consequence — it does not re-derive it.
    """
    report(
        live, consumer, state="sleeping", expected_wake_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    assert listener_verdict(state_of(live).liveness_state) == "healthy"

    assert raise_event(live, consumer, code=EVENT_LISTENER_MISSED_WAKE_WINDOW) is (
        ReportOutcome.EVENT
    )

    row = state_of(live)
    assert row.liveness_state == "offline"
    assert listener_verdict(row.liveness_state) == "offline"
    assert row.expected_wake_at is None, "the promise is spent once it is missed"


def test_the_platform_never_computes_a_wake_window(consumer, live):
    """THE property (spec 6.5, and the phase doc's "never recomputes").

    A wake time an hour in the past, with no event behind it. The Aggregator
    has not said anything is wrong — perhaps the Listener came back and the
    report is simply in flight, perhaps the grace period is long. The platform
    holds a clock and a promise, and must still do NOTHING with them.
    """
    long_past = datetime.now(UTC) - timedelta(hours=1)
    report(live, consumer, state="sleeping", expected_wake_at=long_past)

    row = state_of(live)
    assert row.liveness_state == "sleeping", (
        "the platform decided a wake window had been missed on its own — that "
        "decision belongs to the Aggregator, which holds the only clock that matters"
    )
    assert listener_verdict(row.liveness_state) == "healthy"


def test_an_unrelated_event_does_not_touch_liveness(consumer, live):
    """`listener_stream_gap` is spec 7.3's OTHER Listener event and means
    something different: a gap while streaming, not a missed wake window. Only
    the named code flips the verdict."""
    report(live, consumer, state="streaming")

    raise_event(live, consumer, code=EVENT_LISTENER_STREAM_GAP)

    assert state_of(live).liveness_state == "streaming"


def test_a_missed_wake_for_a_listener_that_never_reported_stores_the_event_only(consumer, live):
    """The event is evidence and is kept. Inventing a `device_state` row from
    it would put a device in the reported table that has never reported."""
    assert raise_event(live, consumer, code=EVENT_LISTENER_MISSED_WAKE_WINDOW) is (
        ReportOutcome.EVENT
    )

    assert state_of(live) is None
    with live() as db:
        assert db.scalars(select(DeviceEvent)).first() is not None


def test_a_redelivered_missed_wake_event_is_not_a_second_outage(consumer, live):
    """QoS 1 is at-least-once. The dedupe is E3.5's; this asserts the liveness
    consequence rides with it rather than being applied twice."""
    report(
        live, consumer, state="sleeping", expected_wake_at=datetime.now(UTC) + timedelta(minutes=1)
    )
    at = datetime.now(UTC)
    raise_event(live, consumer, code=EVENT_LISTENER_MISSED_WAKE_WINDOW, at=at)
    changed = state_of(live).liveness_changed_at

    assert raise_event(live, consumer, code=EVENT_LISTENER_MISSED_WAKE_WINDOW, at=at) is (
        ReportOutcome.DUPLICATE_EVENT
    )

    assert state_of(live).liveness_changed_at == changed


def test_the_listener_can_come_back(consumer, live):
    """Offline is not terminal: spec 6.5 has no absorbing state, and a
    Listener that wakes late still wakes."""
    report(
        live, consumer, state="sleeping", expected_wake_at=datetime.now(UTC) + timedelta(minutes=1)
    )
    raise_event(live, consumer, code=EVENT_LISTENER_MISSED_WAKE_WINDOW)
    assert state_of(live).liveness_state == "offline"

    report(live, consumer, state="streaming", last_audio_at=datetime.now(UTC))

    row = state_of(live)
    assert row.liveness_state == "streaming"
    assert listener_verdict(row.liveness_state) == "healthy"


# --- The timeline -----------------------------------------------------------


def test_the_liveness_journey_lands_on_the_timeline(consumer, live):
    """ACCEPTANCE (phase doc E3.9): "state transitions land on the timeline".

    E3.11 owns rendering. What E3.9 owes it is durable, ordered, machine
    readable evidence, and this walks the whole spec 6.5 cycle to prove the
    rows exist: streaming, then sleeping with a declared wake time, then the
    Aggregator's missed-wake announcement, then recovery.
    """
    t0 = datetime.now(UTC)
    report(live, consumer, state="streaming", reported_at=t0)
    report(
        live,
        consumer,
        state="sleeping",
        expected_wake_at=t0 + timedelta(minutes=5),
        reported_at=t0 + timedelta(minutes=1),
    )
    raise_event(
        live, consumer, code=EVENT_LISTENER_MISSED_WAKE_WINDOW, at=t0 + timedelta(minutes=6)
    )
    report(live, consumer, state="streaming", reported_at=t0 + timedelta(minutes=7))

    with live() as db:
        events = list(db.scalars(select(DeviceEvent).order_by(DeviceEvent.at)).all())
    assert [e.code for e in events] == [EVENT_LISTENER_MISSED_WAKE_WINDOW]
    assert events[0].listener_mac == MAC
    assert events[0].detail, "the Aggregator's own words survive for the timeline to show"

    row = state_of(live)
    assert row.liveness_state == "streaming"
    assert row.liveness_changed_at is not None
