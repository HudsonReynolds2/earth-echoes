"""SIM.2 acceptance: Listener liveness against a real broker and a real platform.

The phase document's acceptance for this task is four claims, and this file is
each of them against the platform that has to believe them:

* a sleeping Listener reads as HEALTHY (spec 9.3, and the easiest thing in the
  system to get wrong — a duty-cycled fleet is silent most of its life);
* a missed wake window flips it offline and lands on the timeline as an event;
* `expected_wake_at` is present exactly while sleeping;
* a `listener_stream_gap` under `capture.mode=continuous` is distinguishable
  from an expected off-window.

Plus the mechanism underneath all of them: a Listener revision reaching
`applied` with nothing hand-driven, having travelled platform -> retained
`lst/{mac}/desired` -> the Aggregator -> the local link -> the Listener ->
`lst/{mac}/reported` -> the platform's consumer.

`test_listener_local_link.py` holds the same state machine's arithmetic
without containers. What is here is what only a real platform can answer:
whether it AGREES.
"""

import uuid

import pytest
from app.contracts.mqtt import EVENT_LISTENER_MISSED_WAKE_WINDOW, EVENT_LISTENER_STREAM_GAP
from app.controlplane.liveness import listener_verdict
from app.controlplane.revision_state import RevisionState
from app.models import DeviceEvent as DeviceEventRow
from conftest import (
    AGG,
    DEP,
    MAC,
    SECOND_MAC,
    apply_config,
    deployment_id_of,
    device_login,
    eventually,
    listener_state,
    operator,
    revision,
    wait_for_state,
    worker_for,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from device import CONTINUOUS, LocalLinkError, MockAggregator

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

#: Spec 5.3's default `listener.wake_grace_seconds`, which is what an
#: Aggregator here is running: the demo hierarchy overrides nothing, so the
#: snapshot every device applies carries the catalogue default. Written out
#: because the WAIT below has to be longer than it, and a reader deserves to
#: know why the number is what it is.
GRACE_SECONDS = 30


async def connected(platform, macs=(MAC, SECOND_MAC)) -> MockAggregator:
    """An Aggregator holding `macs`, attached BEFORE connect so the retained
    Listener desired messages arrive with the subscriptions."""
    device = MockAggregator(deployment_slug=DEP, aggregator_uuid=AGG, login=device_login(platform))
    for mac in macs:
        await device.add_listener(mac)
    await device.connect()
    return device


def revision_id_of(revisions, target_type: str = "listener") -> uuid.UUID:
    """The revision an apply cut for one kind of device. A listener-level
    change cuts exactly one; naming the type keeps that assumption visible."""
    return uuid.UUID(next(r for r in revisions if r["target_type"] == target_type)["revision_id"])


def events_for(platform, mac: str, code: str):
    def probe():
        with platform.factory() as db:
            return db.scalars(
                select(DeviceEventRow).where(
                    DeviceEventRow.code == code, DeviceEventRow.listener_mac == mac
                )
            ).all()

    return probe


# --- the mechanism ----------------------------------------------------------


async def test_a_listener_revision_reaches_applied_over_the_local_link(platform):
    """ACCEPTANCE (phase doc SIM.2): the Aggregator receives its Listener's
    desired config on the `lst/{mac}` subtopic, applies it over the modelled
    local link, and reports on its behalf.

    Nothing is hand-driven and nothing is published on the Listener's own
    behalf, because a Listener has no session to publish with (spec 6.4). The
    revision moves because the checksum the LISTENER computed over what it
    holds matched the one the platform cut.
    """
    with TestClient(platform.app):
        client = operator(platform.app)
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, macs=(MAC,))
            try:
                listener = device.listener(MAC)
                revisions = await apply_config(
                    client,
                    entity_type="listener",
                    ids=[MAC],
                    changes={"audio.sample_rate_hz": 96000},
                )
                revision_id = revision_id_of(revisions)

                await listener.wait_for_apply(revision_id)

                assert listener.config["audio.sample_rate_hz"] == 96000
                assert revision_id not in device.applied_revision_ids, (
                    "the Aggregator applied its Listener's revision to itself; the two "
                    "are different devices with different desired topics"
                )
                assert listener.checksum == revision(platform.factory, revision_id)[1]
                await wait_for_state(platform.factory, revision_id, RevisionState.APPLIED)
            finally:
                await device.disconnect()


# --- ACCEPTANCE: sleeping is healthy ----------------------------------------


async def test_a_sleeping_listener_reads_as_healthy_on_the_platform(platform):
    """ACCEPTANCE (phase doc SIM.2), and the rule the whole state machine
    exists for.

    A duty-cycled Listener is silent for most of its life BY DESIGN. The
    Listener declares where it went and when it will be back, the Aggregator
    passes that on, and the platform paints it healthy — because a platform
    that painted expected silence as failure would report a correctly working
    deployment as a fleet-wide outage every single night.
    """
    with TestClient(platform.app):
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, macs=(MAC,))
            try:
                # Far beyond the grace period: this test is about healthy
                # sleep, and a wake time the device could reach mid-test would
                # be asserting the next test's subject.
                wake = await device.listener(MAC).declare_sleep(seconds=3600)

                stored = await eventually(
                    lambda: sleeping_state(platform, MAC), what=f"recorded {MAC} as sleeping"
                )
                assert listener_verdict(stored.liveness_state) == "healthy", (
                    "the platform called an expected off-window a fault"
                )
                assert stored.expected_wake_at == wake, (
                    "the wake time the Listener declared did not survive the trip; "
                    "without it the platform cannot tell healthy sleep from silence"
                )
            finally:
                await device.disconnect()


def sleeping_state(platform, mac: str):
    row = listener_state(platform.factory, mac)
    return row if row is not None and row.liveness_state == "sleeping" else None


async def test_the_wake_time_is_present_exactly_while_sleeping(platform):
    """ACCEPTANCE (phase doc SIM.2), on the platform's side of the wire.

    The stored `expected_wake_at` appears with the off-window and is gone the
    moment the Listener is back. A stale one surviving the wake would be a
    time some later feature compares a clock against, and spec 6.5 is explicit
    that only the Aggregator may do that.
    """
    with TestClient(platform.app):
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, macs=(SECOND_MAC,))
            listener = device.listener(SECOND_MAC)
            try:
                await listener.declare_sleep(seconds=3600)
                await eventually(
                    lambda: sleeping_state(platform, SECOND_MAC),
                    what=f"recorded {SECOND_MAC} as sleeping",
                )

                await listener.resume_streaming()

                awake = await eventually(
                    lambda: streaming_state(platform, SECOND_MAC),
                    what=f"recorded {SECOND_MAC} as streaming again",
                )
                assert awake.expected_wake_at is None
                assert awake.last_audio_at is not None
                assert listener_verdict(awake.liveness_state) == "healthy"
            finally:
                await device.disconnect()


def streaming_state(platform, mac: str):
    row = listener_state(platform.factory, mac)
    return row if row is not None and row.liveness_state == "streaming" else None


# --- ACCEPTANCE: the missed window ------------------------------------------


async def test_a_missed_wake_window_flips_the_listener_offline_on_the_timeline(platform):
    """ACCEPTANCE (phase doc SIM.2): the missed window, end to end.

    The Listener promises to be back in a second and never is. Nobody tells
    the platform to expect that — it holds a wake time and a clock and does
    NOTHING with them, by design (spec 6.5). The Aggregator waits out its own
    `listener.wake_grace_seconds`, raises the event, and reports the Listener
    offline; the platform records the announcement and the timeline gets the
    durable evidence E3.11 renders.

    This test genuinely waits the spec 5.3 default grace out, which is what
    makes it evidence: shortening it would mean publishing a different
    `listener.wake_grace_seconds` first, and then the thing under test would be
    the config path rather than the liveness one.
    """
    with TestClient(platform.app):
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, macs=(MAC,))
            listener = device.listener(MAC)
            try:
                assert device.wake_grace_seconds == GRACE_SECONDS, (
                    "this device is running a grace period other than the catalogue "
                    "default, so the wait below is measuring the wrong thing"
                )
                await listener.declare_sleep(seconds=1)
                await eventually(
                    lambda: sleeping_state(platform, MAC), what=f"recorded {MAC} as sleeping"
                )

                # The device's own sweep, on the device's own clock. Nothing
                # in this test raises the event or moves the state.
                await listener.wait_for_liveness("offline", timeout=GRACE_SECONDS + 30)

                offline = await eventually(
                    lambda: offline_state(platform, MAC),
                    what=f"recorded {MAC} as offline",
                    timeout=60.0,
                )
                assert listener_verdict(offline.liveness_state) == "offline"
                assert offline.expected_wake_at is None, "the promise is spent once it is missed"

                announcements = await eventually(
                    events_for(platform, MAC, EVENT_LISTENER_MISSED_WAKE_WINDOW),
                    what="stored the missed-wake announcement",
                )
                assert len(announcements) == 1, "one missed window, one event"
                assert announcements[0].level == "warn"
                assert announcements[0].detail, (
                    "the Aggregator's own words are what the timeline shows an operator"
                )
            finally:
                await device.disconnect()


def offline_state(platform, mac: str):
    row = listener_state(platform.factory, mac)
    return row if row is not None and row.liveness_state == "offline" else None


# --- ACCEPTANCE: a gap is not an off-window ---------------------------------


async def test_a_stream_gap_under_continuous_capture_is_not_an_off_window(platform):
    """ACCEPTANCE (phase doc SIM.2), the fourth claim, told as the contrast.

    Two Listeners under one Aggregator. One is switched to
    `capture.mode=continuous` by a real config apply and reports a gap; the
    other stays duty-cycled and declares an off-window. What the platform ends
    up holding is different in every respect that matters: an event and a
    healthy `streaming` device against no event, a `sleeping` device and a
    wake time. That is the distinction spec 6.5 turns on, and it survives the
    whole round trip.
    """
    with TestClient(platform.app):
        client = operator(platform.app)
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, macs=(MAC, SECOND_MAC))
            continuous, duty_cycled = device.listener(MAC), device.listener(SECOND_MAC)
            try:
                revisions = await apply_config(
                    client,
                    entity_type="listener",
                    ids=[MAC],
                    changes={"capture.mode": CONTINUOUS},
                )
                await continuous.wait_for_apply(revision_id_of(revisions))
                assert continuous.capture_mode == CONTINUOUS

                # The continuous one: a gap in a stream that should be running.
                await continuous.report_stream_gap(gap_ms=240)
                # The duty-cycled one: an off-window, declared in advance.
                await duty_cycled.declare_sleep(seconds=3600)

                gaps = await eventually(
                    events_for(platform, MAC, EVENT_LISTENER_STREAM_GAP),
                    what="stored the stream-gap event",
                )
                assert gaps[0].level == "warn"

                still_streaming = await eventually(
                    lambda: streaming_state(platform, MAC),
                    what=f"kept {MAC} streaming through its gap",
                )
                assert listener_verdict(still_streaming.liveness_state) == "healthy"
                assert still_streaming.expected_wake_at is None

                asleep = await eventually(
                    lambda: sleeping_state(platform, SECOND_MAC),
                    what=f"recorded {SECOND_MAC} as sleeping",
                )
                assert listener_verdict(asleep.liveness_state) == "healthy"
                assert asleep.expected_wake_at is not None
                assert events_for(platform, SECOND_MAC, EVENT_LISTENER_STREAM_GAP)() == [], (
                    "an expected off-window was reported as a stream gap, which is the "
                    "one confusion spec 6.5 exists to prevent"
                )

                # And the device refuses to confuse them at the source.
                with pytest.raises(LocalLinkError):
                    await continuous.declare_sleep(seconds=60)
            finally:
                await device.disconnect()
