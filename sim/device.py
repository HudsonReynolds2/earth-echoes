"""The mock Aggregator and its Listeners (tasks SIM.1, SIM.2; spec 6.4, 6.5, 7.1-7.4).

**A client of the platform, never a part of it.** The only platform module
imported here is the published wire contract, `app.contracts.mqtt`. Every
topic string and every control-plane body comes out of its builders and its
models — a hand-rolled topic f-string would be a defect even when it happened
to be correct, because the point of this harness is to prove the published
contract is sufficient for a device that was written against nothing else.

What one of these is, on the wire (spec 7.2's table read as a device reads it):

* it registers an `offline` `StatusMessage` as its LWT **before** connecting,
  so a death the broker notices is a death the platform hears about, and it
  publishes `online` retained once it is up;
* it subscribes to its own `desired` and `cmd` topics — nothing else; the ACL
  grants it nothing else, and a device cannot publish its own desired config;
* it applies a `DesiredConfig` by copying `config` VERBATIM and reports back a
  `ReportedAggregatorState` carrying the checksum IT computed over what IT
  applied (`sim/checksum.py`, the reimplemented D52 recipe);
* it runs a `Command` once per `command_id`, never once per command name;
* QoS 1 everywhere, retain exactly where spec 7.2 says retain.

`MockListener` (task SIM.2) is the other half, and it holds **no MQTT session
of its own** (spec 6.4): it is a child object of the Aggregator that owns it,
reached only by in-process calls. Those calls ARE the local link — spec 17
item 2 still owns the framing of the real HaLow one, so this file models it as
a method call and deliberately invents no wire format for it. Everything a
Listener says reaches the broker through its parent: the parent subscribes to
`lst/{mac}/desired`, hands the config down the local link, and publishes
`ReportedListenerState` on the Listener's behalf.

Spec 6.5's liveness lives here for the same reason it lives in an Aggregator in
the field: the AGGREGATOR compares its own clock to the wake time its Listener
declared, adds `listener.wake_grace_seconds` from its own config, and raises
`listener_missed_wake_window`. Not the platform, which never recomputes a wake
schedule, and not the Listener, which by then is not talking to anyone.
"""

import asyncio
import contextlib
import logging
import socket as socketlib
import ssl
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal, Self, cast

import aiomqtt

from checksum import config_checksum

# The harness reaches the platform by PATH rather than by packaging (phase doc
# SIM.1 fixed choice); pytest is told the same thing through `pythonpath`, and
# this is what makes a plain `python fleet.py` work as well. `backend/alembic/
# env.py` does exactly this, for exactly this reason.
_BACKEND = str(Path(__file__).resolve().parent.parent / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.contracts.mqtt import (  # noqa: E402  (the path above has to be set first)
    EVENT_LISTENER_MISSED_WAKE_WINDOW,
    EVENT_LISTENER_STREAM_GAP,
    QOS,
    Command,
    CommandName,
    DesiredConfig,
    DeviceEvent,
    EventCode,
    HealthBlock,
    ListenerLiveness,
    LivenessState,
    ReportedAggregatorState,
    ReportedListenerState,
    StatusMessage,
    TopicKind,
    command_topic,
    decode,
    desired_topic,
    encode,
    event_topic,
    listener_desired_topic,
    listener_reported_topic,
    parse_topic,
    reported_topic,
    status_topic,
)

log = logging.getLogger(__name__)

#: Seconds between device PINGREQs. Short enough that a fleet member's death
#: is noticed within a test's patience and a demo's attention span; the
#: platform uses the same figure for the same reason (E3.2).
KEEPALIVE_SECONDS: Final = 30

#: Spec 7.3's `health.coarse` is free text, and this is the harness's word for
#: "nothing wrong". SIM.3 is where a scenario gets to say otherwise.
HEALTHY: Final = "ok"

EventLevel = Literal["debug", "info", "warn", "error"]

#: What a SIM.3 scenario may do to a config on its way into a device: given
#: the desired snapshot, return what the device ACTUALLY ends up running. The
#: device then reports that dict with the checksum of that dict, which is how
#: an apply error and drift are produced the way a real device produces them —
#: by holding something else and saying so, never by editing the platform.
ApplyHook = Callable[[dict[str, Any]], dict[str, Any]]

#: The two config keys an Aggregator reads to run spec 6.5, by name because a
#: device holds settings and not a catalogue. `listener.wake_grace_seconds` is
#: an AGGREGATOR-level setting (spec 5.3) — the grace belongs to the thing that
#: applies it — and `capture.mode` is per Listener.
WAKE_GRACE_KEY: Final = "listener.wake_grace_seconds"
CAPTURE_MODE_KEY: Final = "capture.mode"

#: Spec 5.3's defaults, used when a device has been told nothing yet. Written
#: out rather than imported from `app.config.catalog`: firmware ships with its
#: own defaults and only learns the platform's when a revision arrives, so a
#: harness that read the catalogue would hide the day the two disagree.
DEFAULT_WAKE_GRACE_SECONDS: Final = 30.0
DEFAULT_CAPTURE_MODE: Final = "duty_cycle"

#: Spec 6.5: under `capture.mode=continuous` no wake window applies at all, so
#: an off-window declaration from such a Listener is a device contradicting its
#: own configuration rather than a state the platform should ever see.
CONTINUOUS: Final = "continuous"

#: How often an Aggregator compares its clock to the wake times it is holding.
#: Spec 6.5 makes this the ONLY liveness work in the system — there is nothing
#: to send and nothing to poll — so it is cheap enough to run often, and often
#: is what makes `listener_missed_wake_window` land near the grace expiry
#: rather than up to a sweep later.
WAKE_SWEEP_SECONDS: Final = 0.5

#: How long a killed device waits for its own client teardown before giving up
#: on it. A `kill()` leaves aiomqtt holding a socket that will never answer;
#: without a bound, a scenario that models a crash could hang the fleet it was
#: supposed to be testing.
SHUTDOWN_TIMEOUT: Final = 10.0


class LocalLinkError(RuntimeError):
    """A Listener told its Aggregator something a Listener cannot mean.

    The local link is modelled as a method call (spec 17 item 2 owns the real
    framing), so these are the errors that link's protocol would raise: a
    continuous-capture Listener declaring an off-window, or a sleeping one
    reporting a gap in a stream it is not producing. Refused rather than
    published, because the platform's whole ability to tell expected silence
    from failure rests on the two never being confused at the source.
    """


@dataclass(frozen=True)
class BrokerLogin:
    """One Aggregator's broker credential, as `app.devbroker` minted it.

    `password` is excluded from the repr because these objects are logged and
    exception-formatted freely (rule R2); `accounts.json` is the one place the
    value lives, and SIM.4 is what reads it from there.
    """

    host: str
    port: int
    username: str
    password: str = field(repr=False)
    #: The deployment's private CA. None falls back to the public trust store,
    #: which is what a managed broker with a public certificate needs (E5).
    ca_cert: Path | None = None

    def __str__(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"


def tls_context(ca_cert: Path | None) -> ssl.SSLContext:
    """The device's TLS context (spec 7.1: the broker is TLS-only).

    With a CA supplied the context trusts THAT CA AND NOTHING ELSE — not the
    system trust store as well. A deployment's broker is identified by its own
    private CA, and widening the anchor set to every public root would make any
    certificate for the broker's hostname verify too, which is a strictly
    weaker check than the one the pinned PEM exists to make. Hostname
    verification stays on in both branches: a mock that disabled it would be
    testing a connection no real device is allowed to make.
    """
    if ca_cert is None:
        context = ssl.create_default_context()
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cafile=str(ca_cert))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class MockListener:
    """One simulated Listener, owned by its Aggregator (task SIM.2; spec 6.4, 6.5).

    **It holds no MQTT session and no credential**, and that is structural
    rather than decorative: there is no client on this object to publish with,
    so every one of these methods ends in a call to the parent. A Listener that
    could reach the broker on its own would let the harness prove things about
    a topology the platform does not have.

    Everything below is the local link, modelled as an in-process call:

    * `apply` — the parent hands down a `DesiredConfig` that arrived on
      `lst/{mac}/desired` (spec 6.4: "the Aggregator holds its Listeners'
      desired config, applies it to them over the local link");
    * `declare_sleep` — spec 6.5's wake declaration, the message a Listener
      sends BEFORE an off-window. The Listener computes the time from its own
      clock and the Aggregator trusts it, because the Listener's clock is the
      one that governs when it actually wakes;
    * `resume_streaming` — it woke up and there is audio again;
    * `report_stream_gap` — a gap in a stream that was supposed to be running,
      which is spec 7.3's `listener_stream_gap` and is emphatically not an
      off-window.

    Nothing here ever decides that a wake window was MISSED. That decision
    belongs to the Aggregator (`check_wake_windows`), which still has a clock
    when this device does not.
    """

    def __init__(self, *, mac: str, aggregator: "MockAggregator") -> None:
        self.mac = mac
        self.aggregator = aggregator

        #: What this Listener is actually running, copied verbatim from the
        #: desired payload for the reason `MockAggregator._apply` gives.
        self.config: dict[str, Any] = {}
        self.applied_revision_ids: list[uuid.UUID] = []
        self.published_reports = 0

        #: Spec 6.5's three states, as tracked by the parent. A Listener that
        #: has just been attached is on the air until it says otherwise:
        #: `sleeping` would claim a wake time nobody declared, and `offline`
        #: would accuse a device that has done nothing wrong.
        self.state: LivenessState = "streaming"
        self.last_audio_at: datetime | None = None
        self.expected_wake_at: datetime | None = None

    def __str__(self) -> str:
        return f"listener {self.mac} (on {self.aggregator.aggregator_uuid})"

    @property
    def checksum(self) -> str:
        """The D52 checksum of what this Listener is running, computed here for
        the reason `MockAggregator.checksum` gives: echoing the platform's own
        checksum back would make drift undetectable by construction."""
        return config_checksum(self.config)

    @property
    def applied_revision_id(self) -> uuid.UUID | None:
        return self.applied_revision_ids[-1] if self.applied_revision_ids else None

    @property
    def capture_mode(self) -> str:
        """`capture.mode` as this Listener holds it, or spec 5.3's default.

        A Listener that has not been sent a revision yet is running the
        firmware default, which is what `duty_cycle` here means — not "we do
        not know". Reading it off the applied config rather than off the
        desired payload is the point: an off-window is only legal for the mode
        the device is ACTUALLY in.
        """
        mode = self.config.get(CAPTURE_MODE_KEY, DEFAULT_CAPTURE_MODE)
        return mode if isinstance(mode, str) else DEFAULT_CAPTURE_MODE

    @property
    def liveness(self) -> ListenerLiveness:
        """The spec 7.3 liveness block for this Listener, right now.

        `expected_wake_at` is passed through exactly as held, which the
        contract model then checks: present while `sleeping`, absent
        otherwise. The two invariants are maintained by the transitions below,
        so a payload that fails validation here means one of them has a bug —
        and it fails at this device rather than at the platform.
        """
        return ListenerLiveness(
            state=self.state,
            last_audio_at=self.last_audio_at,
            expected_wake_at=self.expected_wake_at,
        )

    # --- the local link (an in-process call, never a wire format) -----------

    async def apply(self, desired: DesiredConfig) -> None:
        """Apply one Listener revision, pushed down the local link.

        Verbatim, for the same load-bearing reason as the Aggregator's own
        apply, and reported by the parent immediately: the platform has no
        other way to learn that a Listener converged.
        """
        self.config = dict(desired.config)
        self.applied_revision_ids.append(desired.revision_id)
        log.info("%s applied revision %s", self, desired.revision_id)
        await self.report()

    async def declare_sleep(
        self, *, seconds: float | None = None, wake_at: datetime | None = None
    ) -> datetime:
        """Spec 6.5's wake declaration: "back at T", sent before an off-window.

        Refused under `capture.mode=continuous`, where spec 6.5 says no wake
        window applies at all. Allowing it would let the harness manufacture a
        healthy-looking silence that no correctly configured Listener can
        produce — and an expected off-window is precisely what an unexpected
        gap has to be distinguishable from.
        """
        if self.capture_mode == CONTINUOUS:
            raise LocalLinkError(
                f"{self} is capturing continuously and cannot declare an off-window "
                "(spec 6.5: no wake window applies under capture.mode=continuous)"
            )
        if (seconds is None) == (wake_at is None):
            raise LocalLinkError("declare_sleep takes exactly one of seconds or wake_at")
        declared = wake_at or datetime.now(UTC) + timedelta(seconds=seconds or 0.0)
        self.state = "sleeping"
        self.expected_wake_at = declared
        log.info("%s declared an off-window, back at %s", self, declared.isoformat())
        await self.report()
        return declared

    async def resume_streaming(self) -> None:
        """It woke up. The promise is spent, so the wake time goes with it —
        a stale `expected_wake_at` is a time something downstream would
        eventually compare a clock against."""
        self.state = "streaming"
        self.expected_wake_at = None
        self.last_audio_at = datetime.now(UTC)
        await self.report()

    async def report_stream_gap(self, *, gap_ms: int) -> DeviceEvent:
        """Spec 7.3's `listener_stream_gap`: audio stopped when it should not have.

        Refused while asleep, and that refusal is the acceptance criterion
        rather than a nicety: a gap during a declared off-window is the
        off-window, and a device that reported it as a fault would teach the
        platform to alert on every duty cycle of every night. The event is
        raised by the parent — Listeners have no session — and deliberately
        does NOT move liveness: the Listener is still streaming, badly.
        """
        if self.state != "streaming":
            raise LocalLinkError(
                f"{self} is {self.state} and cannot report a gap in a stream it is not "
                "producing; an expected off-window is not a stream gap (spec 6.5)"
            )
        return await self.aggregator.publish_event(
            EVENT_LISTENER_STREAM_GAP,
            level="warn",
            detail=f"listener {self.mac} gap {gap_ms}ms",
            listener_mac=self.mac,
        )

    async def report(self) -> ReportedListenerState:
        """Publish this Listener's state — through the parent, always."""
        return await self.aggregator.report_listener(self)

    # --- waiting ------------------------------------------------------------

    async def wait_for_apply(
        self, revision_id: uuid.UUID | None = None, *, timeout: float = 30.0
    ) -> uuid.UUID:
        """Block until this Listener has applied `revision_id` (or anything).

        Shares the parent's progress event: everything this object does was
        done by its parent, so there is exactly one thing to wait on.
        """
        if revision_id is None:
            await self.aggregator.wait_until(
                lambda: bool(self.applied_revision_ids),
                timeout,
                f"applied anything for {self}",
            )
            return self.applied_revision_ids[-1]
        await self.aggregator.wait_until(
            lambda: revision_id in self.applied_revision_ids,
            timeout,
            f"applied {revision_id} for {self}",
        )
        return revision_id

    async def wait_for_liveness(self, state: LivenessState, *, timeout: float = 60.0) -> None:
        """Block until the parent has this Listener in `state` — which for
        `offline` means waiting out the grace period the Aggregator is
        applying, rather than asserting the outcome of a sleep the caller
        already knows it declared."""
        await self.aggregator.wait_until(
            lambda: self.state == state, timeout, f"tracked {self} as {state}"
        )


class MockAggregator:
    """One simulated Aggregator, holding one MQTT session (spec 6.4).

    Usage:

        async with MockAggregator(deployment_slug=..., aggregator_uuid=..., login=...) as agg:
            await agg.wait_for_apply(revision_id)

    A fleet is concurrency, not processes (phase doc fixed choice): every
    instance is an asyncio client, and twenty of them share one event loop.
    """

    def __init__(
        self,
        *,
        deployment_slug: str,
        aggregator_uuid: str,
        login: BrokerLogin,
        keepalive: int = KEEPALIVE_SECONDS,
        identifier: str | None = None,
        wake_sweep_interval: float = WAKE_SWEEP_SECONDS,
    ) -> None:
        self.deployment_slug = deployment_slug
        self.aggregator_uuid = aggregator_uuid
        self.login = login
        self._keepalive = keepalive
        self._wake_sweep_interval = wake_sweep_interval
        # One client id per device, derived from the identity the broker
        # already knows it by: two clients sharing an id evict each other, and
        # a fleet whose members kept stealing one session would look like a
        # flapping network rather than a naming collision.
        self.identifier = identifier or f"sim-{aggregator_uuid}"

        #: What this device is ACTUALLY running: the last `DesiredConfig.config`
        #: copied verbatim. Empty until it has been told anything.
        self.config: dict[str, Any] = {}
        #: Every revision applied, in order — the whole history, because a
        #: scenario asserting "it applied the second one too" needs more than
        #: the latest.
        self.applied_revision_ids: list[uuid.UUID] = []
        #: Commands actually run, in order. Redeliveries are not in here.
        self.commands_executed: list[CommandName] = []
        self.published_reports = 0
        #: The Listeners this Aggregator holds config for and reports on
        #: (SIM.2), keyed by normalized MAC. Insertion-ordered, so "the first
        #: Listener" means the first one attached rather than an arbitrary one.
        self.listeners: dict[str, MockListener] = {}
        #: SIM.3's hook into what this device does with a config it was told
        #: to apply. None — apply it verbatim — is the only correct behaviour
        #: and the default; a scenario installs something else to model
        #: firmware that could not honour a setting. It lives here rather than
        #: inside a scenario module so that `_apply` has exactly one code path
        #: and no `if scenario:` branches accumulate in the device.
        self.apply_hook: ApplyHook | None = None
        self._acted_on: set[uuid.UUID] = set()
        self._connected_at: datetime | None = None
        self._client: aiomqtt.Client | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._reader: asyncio.Task[None] | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._killed = False
        # Set whenever this device does something observable, so waiters block
        # instead of polling. One event for every kind of progress: the
        # waiters re-check their own predicate, so a spurious wake costs a
        # comparison and a missed one would cost a timeout.
        self._progress = asyncio.Event()

    def __str__(self) -> str:
        return f"aggregator {self.aggregator_uuid} ({self.deployment_slug})"

    # --- what the device knows about itself ---------------------------------

    @property
    def checksum(self) -> str:
        """The D52 checksum of what this device is running, computed by the
        device. Not read off the desired payload: echoing the platform's own
        checksum back would make every apply agree by definition and turn
        drift detection into a tautology."""
        return config_checksum(self.config)

    @property
    def applied_revision_id(self) -> uuid.UUID | None:
        return self.applied_revision_ids[-1] if self.applied_revision_ids else None

    def has_acted_on(self, command_id: uuid.UUID) -> bool:
        return command_id in self._acted_on

    @property
    def wake_grace_seconds(self) -> float:
        """How long past a declared wake time this Aggregator waits before it
        calls a Listener offline (spec 6.5, `listener.wake_grace_seconds`).

        **Read off this device's own applied config**, so an operator who
        publishes a longer grace changes device behaviour rather than only a
        database row — which is the whole claim the setting makes. A value the
        platform could not have sent falls back to the spec default instead of
        killing the sweep: a device whose liveness detection stopped would look
        like a fleet that never sleeps, and the cause would be invisible.
        """
        value = self.config.get(WAKE_GRACE_KEY, DEFAULT_WAKE_GRACE_SECONDS)
        if isinstance(value, bool) or not isinstance(value, int | float):
            log.warning(
                "%s was sent %s=%r, which is not a number of seconds; using %s",
                self,
                WAKE_GRACE_KEY,
                value,
                DEFAULT_WAKE_GRACE_SECONDS,
            )
            return DEFAULT_WAKE_GRACE_SECONDS
        return float(value)

    # --- the Listeners this device holds config for -------------------------

    async def add_listener(self, mac: str) -> MockListener:
        """Attach one Listener and start listening for its desired config.

        Subscribing HERE rather than in `connect` is what lets a scenario
        attach a Listener to a running device — including one whose MAC
        inventory has filed under somebody else, which is the whole duplicate
        identity story. The subscription is built from the contract's builder,
        so a MAC that could not appear in a legal topic is refused now rather
        than at the first publish.
        """
        listener = MockListener(mac=mac, aggregator=self)
        self.listeners[mac] = listener
        if self._client is not None:
            await self._client.subscribe(
                listener_desired_topic(self.deployment_slug, self.aggregator_uuid, mac), qos=QOS
            )
        return listener

    def listener(self, mac: str) -> MockListener:
        try:
            return self.listeners[mac]
        except KeyError:
            raise KeyError(f"{self} holds no listener {mac}; attach it with add_listener") from None

    def first_listener(self) -> MockListener:
        """The Listener a scenario means when it names none. A device with no
        Listeners at all is a scenario pointed at the wrong device, and saying
        so beats a StopIteration from inside a behaviour."""
        for listener in self.listeners.values():
            return listener
        raise LookupError(f"{self} holds no listeners; this scenario needs at least one")

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Dial the broker, register the will, subscribe, announce `online`.

        The order is the contract, not a preference:

        1. **The will is handed to the constructor**, so it reaches the broker
           in the CONNECT packet. A will registered afterwards does not exist
           as far as the broker is concerned, and a device that died in the
           window would simply go quiet — which spec 9.3 reads as healthy.
        2. **Subscribe before announcing.** `desired` is retained (spec 7.2),
           so the current revision arrives the instant the subscription lands;
           that is spec 6.4's no-polling property, and announcing first would
           have the device racing its own configuration.
        """
        if self._client is not None:
            raise RuntimeError(f"{self} is already connected")
        now = datetime.now(UTC)
        will = aiomqtt.Will(
            topic=status_topic(self.deployment_slug, self.aggregator_uuid),
            payload=encode(StatusMessage(state="offline", at=now)),
            qos=QOS,
            retain=True,
        )
        stack = contextlib.AsyncExitStack()
        client = await stack.enter_async_context(
            aiomqtt.Client(
                hostname=self.login.host,
                port=self.login.port,
                username=self.login.username,
                password=self.login.password,
                identifier=self.identifier,
                tls_context=tls_context(self.login.ca_cert),
                keepalive=self._keepalive,
                clean_session=True,
                will=will,
            )
        )
        self._stack = stack
        self._client = client
        self._connected_at = now
        self._killed = False
        topics = [
            desired_topic(self.deployment_slug, self.aggregator_uuid),
            command_topic(self.deployment_slug, self.aggregator_uuid),
            # Its Listeners' desired topics, one per attached MAC. Retained
            # like its own (spec 7.2), so a Listener's config arrives the
            # moment the subscription lands and the local link has something
            # to carry.
            *(
                listener_desired_topic(self.deployment_slug, self.aggregator_uuid, mac)
                for mac in self.listeners
            ),
        ]
        for topic in topics:
            await client.subscribe(topic, qos=QOS)
        self._reader = asyncio.create_task(self._read(), name=f"{self.identifier}-reader")
        self._sweeper = asyncio.create_task(self._sweep(), name=f"{self.identifier}-wake-sweep")
        await self._announce("online")
        log.info("%s connected as %s", self, self.login)

    async def disconnect(self) -> None:
        """Shut down politely — which means SAYING so.

        MQTT discards the will on a clean DISCONNECT, so a device that merely
        closed its socket would leave `online` retained on its status topic
        forever and the platform would keep painting a machine that is not
        there. The explicit `offline` is also what makes this harness usable
        for testing crashes: an exit that looked like a crash could never be
        told apart from one.
        """
        if self._client is None:
            return
        if not self._killed:
            # A killed device says nothing. That is the whole difference
            # between this and `kill()`, and suppressing the failed publish
            # instead would leave the two paths indistinguishable from the
            # broker's side on a slow link.
            with contextlib.suppress(aiomqtt.MqttError):
                await self._announce("offline")
        for task in (self._reader, self._sweeper):
            if task is not None:
                task.cancel()
                # `MqttError` as well as `CancelledError`: after a kill the
                # reader is already dead of the connection it was reading, and
                # awaiting a task that failed re-raises what killed it. A
                # device that could not be shut down because it had crashed —
                # the one state this harness exists to produce — would be a
                # poor kind of mock.
                with contextlib.suppress(asyncio.CancelledError, aiomqtt.MqttError):
                    await task
        self._reader = self._sweeper = None
        stack, self._stack = self._stack, None
        self._client = None
        self._connected_at = None
        if stack is not None:
            # Bounded: after a kill the client is holding a socket that will
            # never answer, and an unbounded teardown would hang the fleet
            # rather than the one device that was supposed to die.
            with contextlib.suppress(Exception):
                async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                    await stack.aclose()

    async def kill(self) -> None:
        """Die the way a device dies: with the socket gone and nothing said.

        The MQTT will is published by the broker on ANY close that was not a
        DISCONNECT packet, so the difference between this and `disconnect()`
        has to be made at the transport — closing the underlying socket out
        from under the client is the in-process equivalent of the SIGKILL
        `test_mock_aggregator` uses on a detached one. A harness whose only
        exit was polite could never produce the signal spec 9.3 makes
        authoritative.

        This reaches into aiomqtt's paho client, which is a client library and
        not the platform: the harness boundary rule is about not reaching into
        the PLATFORM, and there is no public API in any MQTT library for
        "pretend the power went out".

        It SHUTS DOWN the socket rather than closing it, and the distinction is
        not cosmetic. The broker sees the same thing either way — a transport
        that ended without a DISCONNECT packet, so the will is owed — but a
        closed socket's descriptor goes to -1 while asyncio still holds a
        reader and a writer against it, and the next callback paho registers
        takes the event loop down with `ValueError: Invalid file descriptor:
        -1` rather than this device. Shutting down leaves the descriptor valid
        and dead: writes fail, so nothing polite can escape, and the ordinary
        teardown below closes it once the watchers are gone.
        """
        if self._client is None:
            raise RuntimeError(f"{self} is not connected; there is nothing to kill")
        socket = self._client._client.socket()  # noqa: SLF001  (see the docstring)
        if socket is not None:
            # paho types this as a union covering its websocket wrapper, which
            # this harness never uses: every connection here is TLS over TCP
            # (`connect`), so the object is a real socket and has `shutdown`.
            # Already-dead ones raise (ENOTCONN), and a device that was already
            # dead is in exactly the state this method wanted it in.
            with contextlib.suppress(OSError):
                cast(socketlib.socket, socket).shutdown(socketlib.SHUT_RDWR)
        self._killed = True
        log.info("%s was killed: no DISCONNECT, so the broker owes it a will", self)
        await self.disconnect()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.disconnect()

    # --- publishing ---------------------------------------------------------

    async def report(self) -> ReportedAggregatorState:
        """Publish this device's reported state (spec 7.3), unretained.

        `config` and `checksum` travel together because spec 7.4 makes the
        message idempotent on `applied_revision_id` plus `checksum`, and
        because the platform refuses a device that contradicts itself (D70) —
        the checksum here is genuinely computed over the dict being sent.
        """
        state = ReportedAggregatorState(
            reported_at=datetime.now(UTC),
            applied_revision_id=self.applied_revision_id,
            config=self.config,
            health=HealthBlock(uptime_s=self._uptime_s(), coarse=HEALTHY),
            checksum=self.checksum,
        )
        await self._publish(
            reported_topic(self.deployment_slug, self.aggregator_uuid),
            encode(state),
            retain=False,
        )
        self.published_reports += 1
        return state

    async def report_listener(self, listener: MockListener) -> ReportedListenerState:
        """Publish one Listener's state on its behalf (spec 6.4, 7.3).

        The Aggregator's own topic and the Aggregator's own credential, for a
        device that has neither: `lst/{mac}/reported` is inside this device's
        ACL subtree precisely because the parent is the only thing that can
        speak for it. The liveness block comes from the Listener's tracked
        state and the checksum from the config it actually holds, so the two
        halves of a Listener report are as honest as an Aggregator's own.
        """
        state = ReportedListenerState(
            reported_at=datetime.now(UTC),
            applied_revision_id=listener.applied_revision_id,
            config=listener.config,
            liveness=listener.liveness,
            checksum=listener.checksum,
        )
        await self._publish(
            listener_reported_topic(self.deployment_slug, self.aggregator_uuid, listener.mac),
            encode(state),
            retain=False,
        )
        listener.published_reports += 1
        self._progress.set()
        return state

    async def publish_event(
        self,
        code: EventCode,
        *,
        level: EventLevel = "info",
        detail: str | None = None,
        listener_mac: str | None = None,
    ) -> DeviceEvent:
        """Publish one spec 7.3 `DeviceEvent`, unretained (spec 7.2's table).

        Unretained matters: an event is something that HAPPENED at a moment. A
        retained one would be redelivered on every reconnect and land on the
        timeline again, so a single stream gap would read as an outage that
        never ended.
        """
        event = DeviceEvent(
            at=datetime.now(UTC),
            level=level,
            code=code,
            detail=detail,
            listener_mac=listener_mac,
        )
        await self._publish(
            event_topic(self.deployment_slug, self.aggregator_uuid),
            encode(event),
            retain=False,
        )
        return event

    async def _announce(self, state: Literal["online", "offline"]) -> None:
        """The status topic is RETAINED (spec 7.2), which is the whole design:
        the platform learns the fleet's liveness on connect without asking
        anyone, and an LWT lands in the same place with the same shape."""
        await self._publish(
            status_topic(self.deployment_slug, self.aggregator_uuid),
            encode(StatusMessage(state=state, at=datetime.now(UTC))),
            retain=True,
        )

    async def _publish(self, topic: str, payload: bytes, *, retain: bool) -> None:
        if self._client is None:
            raise RuntimeError(f"{self} is not connected; nothing was published to {topic}")
        await self._client.publish(topic, payload, qos=QOS, retain=retain)

    # --- receiving ----------------------------------------------------------

    async def _read(self) -> None:
        client = self._client
        assert client is not None  # only started from connect(), after the client exists
        async for message in client.messages:
            try:
                await self._deliver(message)
            except Exception:
                # A reader task that dies takes the device's whole inbound half
                # with it, silently: it stops applying and stops answering
                # commands, and reads on the platform as a device that hung
                # rather than as a harness with a bug. Keep the session, and
                # say loudly what broke.
                log.exception("%s could not handle a message on %s", self, message.topic)

    async def _deliver(self, message: aiomqtt.Message) -> None:
        """Route one delivered message by what its TOPIC says it is.

        `parse_topic` rather than string surgery, in both directions: the
        identifiers are validated by the same code that built them, so a
        wildcard that somehow reached a delivered topic is refused instead of
        being taken for a device name.
        """
        topic = parse_topic(str(message.topic))
        payload = bytes(message.payload or b"")
        match topic.kind:
            case TopicKind.AGGREGATOR_DESIRED:
                await self._apply(decode(DesiredConfig, payload))
            case TopicKind.AGGREGATOR_COMMAND:
                self._execute(decode(Command, payload))
            case TopicKind.LISTENER_DESIRED:
                assert topic.mac is not None  # a LISTENER_DESIRED topic always carries one
                await self._apply_listener(topic.mac, decode(DesiredConfig, payload))
            case _:
                # Nothing else is subscribed, and the ACL grants nothing else.
                # Arriving here means a subscription was added without a
                # branch to answer it.
                log.warning("%s has no handler for %s (%s)", self, message.topic, topic.kind)

    async def _apply(self, desired: DesiredConfig) -> None:
        """Apply one revision and report it (spec 6.4 step 3).

        **`config` is copied VERBATIM** — not normalized, not re-serialized,
        not stripped of nulls. That is the load-bearing rule of the contract
        module: the snapshot is the publishable payload body, so a checksum
        computed over an untouched copy of it matches the platform's by
        construction. A device that tidied the snapshot on the way in would
        report a checksum that can never match and would sit in `drifted`
        forever, for a reason invisible from either end.
        """
        applied = dict(desired.config)
        if self.apply_hook is not None:
            # A SIM.3 scenario saying out loud that this device could not
            # honour what it was sent. What comes back is what the device then
            # GENUINELY holds and genuinely computes a checksum over, which is
            # how an apply error is produced the way a device produces one.
            applied = self.apply_hook(applied)
            log.info("%s put revision %s through a scenario apply hook", self, desired.revision_id)
        self.config = applied
        self.applied_revision_ids.append(desired.revision_id)
        log.info("%s applied revision %s", self, desired.revision_id)
        await self.report()
        self._progress.set()

    async def _apply_listener(self, mac: str, desired: DesiredConfig) -> None:
        """Push one Listener revision down the local link and report it back.

        An unknown MAC is logged and dropped rather than auto-attached: this
        device subscribed to exactly the Listeners it holds, so a desired
        message for another one means the platform believes in a Listener this
        Aggregator was never given — which is worth seeing, and worth not
        answering on behalf of.
        """
        listener = self.listeners.get(mac)
        if listener is None:
            log.warning("%s was sent config for %s, which it does not hold", self, mac)
            return
        await listener.apply(desired)
        self._progress.set()

    # --- spec 6.5: the only liveness work in the system ---------------------

    async def check_wake_windows(self, *, now: datetime | None = None) -> list[MockListener]:
        """Compare this device's clock to the wake times it is holding (spec 6.5).

        The Aggregator's decision, made with the Aggregator's own grace period,
        announced in the Aggregator's own words. The platform never recomputes
        a wake schedule and the Listener is by definition not talking, so if
        this method does not raise the event, nothing does.

        The order follows spec 6.5's sentence exactly: raise
        `listener_missed_wake_window`, and report the Listener as offline on
        the next `lst/{mac}/reported` publish. The event is the first news — an
        operator who learned of an outage only at the next report would be told
        the device is fine in the meantime — and the report that follows
        confirms it over the same ordered session.

        Idempotent: a Listener already `offline` is not overdue again, so
        sweeping twice a second does not raise an event twice a second.
        """
        moment = now or datetime.now(UTC)
        grace = timedelta(seconds=self.wake_grace_seconds)
        missed: list[MockListener] = []
        for listener in list(self.listeners.values()):
            wake_at = listener.expected_wake_at
            if listener.state != "sleeping" or wake_at is None or moment < wake_at + grace:
                continue
            listener.state = "offline"
            listener.expected_wake_at = None  # the promise is spent once it is missed
            await self.publish_event(
                EVENT_LISTENER_MISSED_WAKE_WINDOW,
                level="warn",
                detail=(
                    f"listener {listener.mac} expected {wake_at.isoformat()}, "
                    f"grace {self.wake_grace_seconds:g}s elapsed"
                ),
                listener_mac=listener.mac,
            )
            await self.report_listener(listener)
            missed.append(listener)
            log.warning("%s missed its wake window and is offline", listener)
        if missed:
            self._progress.set()
        return missed

    async def _sweep(self) -> None:
        """Run `check_wake_windows` for as long as this device is up.

        A device that noticed missed wake windows only when a test asked it to
        would make spec 6.5 a property of the suite rather than of the fleet,
        and a SIM.3 scenario could not produce one at all. Failures are logged
        and the loop continues, for `_read`'s reason: a sweeper that died
        quietly would leave every sleeping Listener healthy forever.
        """
        while True:
            await asyncio.sleep(self._wake_sweep_interval)
            try:
                await self.check_wake_windows()
            except Exception:
                log.exception("%s could not sweep its wake windows", self)

    def _execute(self, command: Command) -> None:
        """Run one command at most once, deduplicated by `command_id` (spec 7.4).

        By the ID and NEVER by the command name. QoS 1 is at-least-once, so the
        same bytes can arrive twice and must run once; keying on the name
        instead would make an operator's deliberate second restart a silent
        no-op, and the platform would report success for something that never
        happened. `Command.command_id`'s fresh-UUID default is what makes the
        two cases structurally distinguishable.

        What a command DOES to the simulated device is SIM.3's registry. What
        is asserted here is that it ran, and how often.
        """
        if command.command_id in self._acted_on:
            log.info("%s ignored a redelivery of command %s", self, command.command_id)
            return
        self._acted_on.add(command.command_id)
        self.commands_executed.append(command.command)
        log.info("%s ran %s (%s)", self, command.command, command.command_id)
        self._progress.set()

    # --- waiting ------------------------------------------------------------

    async def wait_for_apply(
        self, revision_id: uuid.UUID | None = None, *, timeout: float = 30.0
    ) -> uuid.UUID:
        """Block until this device has applied `revision_id` (or anything).

        Callers wait on the DEVICE rather than sleeping, so a slow container
        cannot make a suite flaky in one direction and slow in the other.
        """
        if revision_id is None:
            await self._wait_until(lambda: bool(self.applied_revision_ids), timeout, "applied")
            return self.applied_revision_ids[-1]
        await self._wait_until(
            lambda: revision_id in self.applied_revision_ids, timeout, f"applied {revision_id}"
        )
        return revision_id

    async def wait_for_command(self, command_id: uuid.UUID, *, timeout: float = 30.0) -> None:
        await self._wait_until(
            lambda: command_id in self._acted_on, timeout, f"ran command {command_id}"
        )

    async def wait_until(self, ready: Callable[[], bool], timeout: float, what: str) -> None:
        """Block until `ready()`, on this device's one progress signal.

        Public because a `MockListener` waits on it: everything a Listener
        does was done by this object, so a second event for the child half
        would be a second thing to forget to set.
        """
        await self._wait_until(ready, timeout, what)

    async def _wait_until(self, ready: Callable[[], bool], timeout: float, what: str) -> None:
        try:
            async with asyncio.timeout(timeout):
                while not ready():
                    # Clear then wait, with no await in between: the setters
                    # run in the reader and sweeper tasks, so neither can
                    # interleave here, and a signal that arrived before the
                    # check is already visible in `ready()`.
                    self._progress.clear()
                    await self._progress.wait()
        except TimeoutError as error:
            raise TimeoutError(f"{self} never {what} within {timeout}s") from error

    def _uptime_s(self) -> int:
        if self._connected_at is None:
            return 0
        return int((datetime.now(UTC) - self._connected_at).total_seconds())
