"""The mock Aggregator (task SIM.1; spec 6.4, 6.5, 7.1-7.4).

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

Listeners are SIM.2's: they hold no MQTT session of their own (spec 6.4), so
they arrive as subtopics of this one, and this class deliberately does not
subscribe to them yet rather than half-implementing them.
"""

import asyncio
import contextlib
import logging
import ssl
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal, Self

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
    QOS,
    Command,
    CommandName,
    DesiredConfig,
    DeviceEvent,
    EventCode,
    HealthBlock,
    ReportedAggregatorState,
    StatusMessage,
    TopicKind,
    command_topic,
    decode,
    desired_topic,
    encode,
    event_topic,
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
    ) -> None:
        self.deployment_slug = deployment_slug
        self.aggregator_uuid = aggregator_uuid
        self.login = login
        self._keepalive = keepalive
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
        self._acted_on: set[uuid.UUID] = set()
        self._connected_at: datetime | None = None
        self._client: aiomqtt.Client | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._reader: asyncio.Task[None] | None = None
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
        for topic in (
            desired_topic(self.deployment_slug, self.aggregator_uuid),
            command_topic(self.deployment_slug, self.aggregator_uuid),
        ):
            await client.subscribe(topic, qos=QOS)
        self._reader = asyncio.create_task(self._read(), name=f"{self.identifier}-reader")
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
        with contextlib.suppress(aiomqtt.MqttError):
            await self._announce("offline")
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        stack, self._stack = self._stack, None
        self._client = None
        self._connected_at = None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

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
        self.config = dict(desired.config)
        self.applied_revision_ids.append(desired.revision_id)
        log.info("%s applied revision %s", self, desired.revision_id)
        await self.report()
        self._progress.set()

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

    async def _wait_until(self, ready: Callable[[], bool], timeout: float, what: str) -> None:
        try:
            async with asyncio.timeout(timeout):
                while not ready():
                    # Clear then wait, with no await in between: the setter runs
                    # in the reader task, so it cannot interleave here, and a
                    # signal that arrived before the check is already visible in
                    # `ready()`.
                    self._progress.clear()
                    await self._progress.wait()
        except TimeoutError as error:
            raise TimeoutError(f"{self} never {what} within {timeout}s") from error

    def _uptime_s(self) -> int:
        if self._connected_at is None:
            return 0
        return int((datetime.now(UTC) - self._connected_at).total_seconds())
