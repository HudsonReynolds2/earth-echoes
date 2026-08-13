"""SIM.2: the local link and the grace arithmetic, with no broker in the way.

Everything in this file is a property of the DEVICE — what a Listener may tell
its Aggregator, what the Aggregator decides with that and its own clock, and
what bytes come out of the decision. It runs without containers because it
needs none: the spec 6.5 state machine is arithmetic over a promise and a
grace period, and asserting it against a stopwatch would make a fast, exact
test into a slow, flaky one. `test_mock_listener.py` is where the same
machine is driven against a real broker and a real platform.

The transport is the only thing stubbed, and only the transport:
`MockAggregator._publish` is replaced with a recorder, so every assertion
below is made against the REAL payloads the device would have put on the wire,
decoded with the contract's own models. Nothing else is faked — the liveness
transitions, the grace comparison and the event are the shipping code's.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from app.contracts.mqtt import (
    EVENT_LISTENER_MISSED_WAKE_WINDOW,
    EVENT_LISTENER_STREAM_GAP,
    DesiredConfig,
    DesiredTarget,
    DeviceEvent,
    ReportedListenerState,
    decode,
    event_topic,
    listener_reported_topic,
    parse_topic,
)

from checksum import config_checksum
from device import (
    CAPTURE_MODE_KEY,
    CONTINUOUS,
    DEFAULT_WAKE_GRACE_SECONDS,
    WAKE_GRACE_KEY,
    BrokerLogin,
    LocalLinkError,
    MockAggregator,
)

DEP = "redwood-coast"
AGG = "demo-agg-rc-01"
MAC = "02:EE:0E:01:01:01"

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class Sent:
    """One publish the device made, as the broker would have received it."""

    topic: str
    payload: bytes
    retain: bool


@pytest.fixture
def wire() -> list[Sent]:
    return []


@pytest.fixture
def device(wire: list[Sent]) -> MockAggregator:
    """An Aggregator with a recorder where its socket would be.

    The credential is nonsense on purpose: nothing here dials anything, and a
    plausible one would invite a later edit to try.
    """
    aggregator = MockAggregator(
        deployment_slug=DEP,
        aggregator_uuid=AGG,
        login=BrokerLogin(host="broker.invalid", port=8883, username="unused", password="unused"),
    )

    async def record(topic: str, payload: bytes, *, retain: bool) -> None:
        wire.append(Sent(topic, payload, retain))

    aggregator._publish = record  # type: ignore[method-assign]  # the transport, and only it
    return aggregator


def reports(wire: list[Sent], mac: str = MAC) -> list[ReportedListenerState]:
    topic = listener_reported_topic(DEP, AGG, mac)
    return [decode(ReportedListenerState, item.payload) for item in wire if item.topic == topic]


def events(wire: list[Sent]) -> list[DeviceEvent]:
    topic = event_topic(DEP, AGG)
    return [decode(DeviceEvent, item.payload) for item in wire if item.topic == topic]


def desired(config: dict[str, object]) -> DesiredConfig:
    """A desired payload for the Listener, exactly as the platform builds one."""
    return DesiredConfig(
        revision_id=uuid.uuid4(),
        generated_at=datetime.now(UTC),
        target=DesiredTarget(type="listener", id=MAC),
        config=config,
        checksum=config_checksum(config),
    )


# --- a Listener has no session ----------------------------------------------


async def test_a_listener_publishes_only_through_its_parent(device, wire):
    """Spec 6.4: Listeners do not connect to MQTT.

    Structural, not decorative — there is no client on the child object to
    publish with, and everything it says arrives on ITS PARENT's topics, under
    the parent's credential. A Listener that could reach a broker on its own
    would let this harness prove things about a topology the platform does not
    have and firmware cannot build.
    """
    listener = await device.add_listener(MAC)
    assert not hasattr(listener, "_client")

    await listener.resume_streaming()
    await listener.report_stream_gap(gap_ms=240)

    assert wire, "the Listener said nothing at all"
    for item in wire:
        assert parse_topic(item.topic).agg == AGG
        assert item.retain is False, "a Listener report is a moment, never retained (spec 7.2)"


async def test_a_listener_applies_its_own_revision_verbatim(device, wire):
    """The local link carrying config downhill (spec 6.4): the parent receives
    it on `lst/{mac}/desired` and hands it to the Listener, which reports the
    checksum IT computed over what it actually holds."""
    listener = await device.add_listener(MAC)
    payload = desired({"audio.sample_rate_hz": 96000, CAPTURE_MODE_KEY: "duty_cycle"})

    await listener.apply(payload)

    assert listener.config == payload.config
    assert listener.applied_revision_id == payload.revision_id
    report = reports(wire)[-1]
    assert report.applied_revision_id == payload.revision_id
    assert report.checksum == payload.checksum, (
        "the device computed a different checksum over the same bytes it was sent; "
        "the D52 recipe and the verbatim copy are what make these equal"
    )


# --- spec 6.5: the wake declaration -----------------------------------------


async def test_the_wake_time_is_present_exactly_while_sleeping(device, wire):
    """ACCEPTANCE (phase doc SIM.2), asserted on the BYTES.

    Spec 7.3: `expected_wake_at` is present only while sleeping. It is absent
    once the Listener resumes and absent once it flips to offline, and the
    contract model refuses every other combination — so this test is also the
    proof that the device's transitions never build a payload it would reject.
    """
    listener = await device.add_listener(MAC)

    await listener.resume_streaming()
    assert reports(wire)[-1].liveness.expected_wake_at is None

    wake = await listener.declare_sleep(seconds=300)
    sleeping = reports(wire)[-1].liveness
    assert sleeping.state == "sleeping"
    assert sleeping.expected_wake_at == wake

    await listener.resume_streaming()
    awake = reports(wire)[-1].liveness
    assert awake.state == "streaming"
    assert awake.expected_wake_at is None, "a spent promise is a time something would compare to"
    assert awake.last_audio_at is not None


async def test_a_continuous_listener_cannot_declare_an_off_window(device):
    """Spec 6.5: under `capture.mode=continuous` no wake window applies.

    Refused at the local link rather than published, because the entire
    distinction the platform relies on is between expected silence and
    unexpected silence — and a continuously-capturing Listener claiming an
    expected off-window would manufacture healthy-looking silence no correctly
    configured device can produce.
    """
    listener = await device.add_listener(MAC)
    await listener.apply(desired({CAPTURE_MODE_KEY: CONTINUOUS}))

    with pytest.raises(LocalLinkError, match="continuously"):
        await listener.declare_sleep(seconds=60)


async def test_a_sleeping_listener_cannot_report_a_stream_gap(device):
    """The other half of the same distinction: a gap during a declared
    off-window IS the off-window. A device that raised `listener_stream_gap`
    there would teach the platform to alert on every duty cycle of every
    night, which is precisely the outcome spec 6.5 exists to prevent."""
    listener = await device.add_listener(MAC)
    await listener.declare_sleep(seconds=60)

    with pytest.raises(LocalLinkError, match="not producing"):
        await listener.report_stream_gap(gap_ms=240)


# --- spec 6.5: the grace period ---------------------------------------------


async def test_the_grace_period_comes_from_the_devices_own_config(device):
    """`listener.wake_grace_seconds` is a SETTING (spec 5.3), so an operator
    who publishes a different one changes what the device does. Read off the
    applied config, which is the only place a device could have got it."""
    assert device.wake_grace_seconds == DEFAULT_WAKE_GRACE_SECONDS

    device.config = {WAKE_GRACE_KEY: 5}
    assert device.wake_grace_seconds == 5.0


async def test_a_nonsense_grace_falls_back_to_the_spec_default(device):
    """A device whose liveness detection stopped would look like a fleet that
    never sleeps, with nothing to see. Fall back loudly and keep sweeping."""
    device.config = {WAKE_GRACE_KEY: "half an hour"}
    assert device.wake_grace_seconds == DEFAULT_WAKE_GRACE_SECONDS


async def test_a_window_is_missed_only_once_the_declared_time_and_grace_have_passed(device, wire):
    """The whole of spec 6.5's third state, in one comparison.

    The Aggregator compares its own clock to the time the Listener promised
    plus its own grace. Before that instant there is nothing wrong — a
    Listener that is late by less than the grace is a Listener that is late —
    and the platform is told nothing, because being told would make it paint a
    healthy device as an outage.
    """
    device.config = {WAKE_GRACE_KEY: 30}
    listener = await device.add_listener(MAC)
    wake = await listener.declare_sleep(seconds=60)

    assert await device.check_wake_windows(now=wake + timedelta(seconds=29)) == []
    assert listener.state == "sleeping"

    missed = await device.check_wake_windows(now=wake + timedelta(seconds=31))

    assert missed == [listener]
    assert listener.state == "offline"
    assert listener.expected_wake_at is None, "the promise is spent once it is missed"


async def test_the_aggregator_announces_the_miss_before_it_reports_it(device, wire):
    """Spec 6.5's sentence, in order: raise `listener_missed_wake_window`, then
    report the Listener offline on the next `lst/{mac}/reported` publish.

    The event first because it is the first news — an operator who learned of
    an outage only at the next report would be told the device was fine in the
    meantime — and both over the same ordered session, so the report confirms
    rather than contradicts.
    """
    device.config = {WAKE_GRACE_KEY: 1}
    listener = await device.add_listener(MAC)
    wake = await listener.declare_sleep(seconds=1)
    wire.clear()

    await device.check_wake_windows(now=wake + timedelta(seconds=2))

    assert [parse_topic(item.topic).kind for item in wire] == [
        "aggregator_event",
        "listener_reported",
    ]
    announcement = events(wire)[0]
    assert announcement.code == EVENT_LISTENER_MISSED_WAKE_WINDOW
    assert announcement.level == "warn"
    assert announcement.listener_mac == MAC
    assert announcement.detail and "grace 1s" in announcement.detail
    confirmation = reports(wire)[0].liveness
    assert confirmation.state == "offline"
    assert confirmation.expected_wake_at is None


async def test_sweeping_again_is_not_a_second_outage(device, wire):
    """The sweep runs twice a second for the life of the device. A Listener
    already offline is not overdue again, or every missed window would become
    an unbounded stream of identical events on the timeline."""
    device.config = {WAKE_GRACE_KEY: 1}
    listener = await device.add_listener(MAC)
    wake = await listener.declare_sleep(seconds=1)
    later = wake + timedelta(seconds=5)

    await device.check_wake_windows(now=later)
    wire.clear()
    assert await device.check_wake_windows(now=later) == []

    assert wire == [], "a second sweep raised a second outage for one missed window"
    assert listener.state == "offline"


async def test_a_listener_that_wakes_up_late_still_wakes_up(device, wire):
    """Spec 6.5 has no absorbing state. Offline is a verdict about now, and a
    Listener that comes back reports `streaming` like any other."""
    device.config = {WAKE_GRACE_KEY: 1}
    listener = await device.add_listener(MAC)
    wake = await listener.declare_sleep(seconds=1)
    await device.check_wake_windows(now=wake + timedelta(seconds=5))

    await listener.resume_streaming()

    assert listener.state == "streaming"
    assert reports(wire)[-1].liveness.state == "streaming"


async def test_a_streaming_listener_is_never_swept_offline(device):
    """Only a declared off-window can be missed. A streaming Listener carries
    no promise, so there is nothing for a clock to be compared against — and
    an Aggregator that invented one would be doing the platform's job badly on
    the platform's behalf."""
    device.config = {WAKE_GRACE_KEY: 1}
    listener = await device.add_listener(MAC)
    await listener.resume_streaming()

    assert await device.check_wake_windows(now=datetime.now(UTC) + timedelta(days=1)) == []
    assert listener.state == "streaming"


async def test_a_stream_gap_is_an_event_and_not_a_liveness_change(device, wire):
    """ACCEPTANCE (phase doc SIM.2): a stream gap is distinguishable from an
    expected off-window, at the source.

    Different code, different shape, different consequence: the gap is an
    event about a stream that IS running, and the Listener stays `streaming`.
    An off-window is a liveness state with a wake time and no event at all.
    """
    listener = await device.add_listener(MAC)
    await listener.apply(desired({CAPTURE_MODE_KEY: CONTINUOUS}))
    wire.clear()

    await listener.report_stream_gap(gap_ms=240)

    assert [item.code for item in events(wire)] == [EVENT_LISTENER_STREAM_GAP]
    assert reports(wire) == [], "a gap is not a liveness change and needs no report"
    assert listener.state == "streaming"
