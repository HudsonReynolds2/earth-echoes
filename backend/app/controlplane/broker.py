"""The MQTT client manager (task E3.2; spec 7.1, 7.2, 7.4).

One long-lived connection per deployment broker, dialled outbound from the
platform (spec 7.1), with the coordinates and credentials read from that
deployment's `deployment_service` MQTT row. Above it sit E3.4's publisher and
E3.5's consumer; below it is aiomqtt (the phase-3 fixed client choice).

The contract this module offers its callers is that **a broker outage is not
an event message-handling code ever sees**. Registered handlers are invoked
with delivered messages and nothing else: no connection-lost callback, no
resubscribe hook, no reconnect bookkeeping. A broker that dies and comes back
produces, from a handler's point of view, a gap in messages and nothing more.
Everything that makes that true — the retry loop, the backoff, and replaying
the whole subscription set onto each new connection — lives in
`_connection_loop` below.

Three properties are load-bearing and easy to lose in a later edit:

1. **Subscriptions are registered before `start()` and replayed on EVERY
   connect.** The set is fixed for the manager's lifetime, so a reconnect
   cannot restore a partial view of the topic namespace.
2. **A handler that raises does not break the connection.** One malformed
   report from one device must not stop a whole deployment's control plane,
   so dispatch logs and continues.
3. **Sessions are clean and subscriptions explicit.** The platform does not
   ask the broker to remember its session: several API replicas may hold the
   same deployment, and a shared persistent session id would have them evict
   each other. Delivery guarantees come from QoS 1 plus the retained desired
   topics (spec 6.4), not from broker-side session state.

Credentials reach here as `BrokerCoordinates.password`, straight from
`SecretStore`. That field is excluded from the dataclass repr and the class
carries a `__str__` that names only the deployment and the socket, because
every log line in this module interpolates a coordinates object (rule R2).
"""

import asyncio
import contextlib
import logging
import random
import ssl
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import aiomqtt
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts.mqtt import QOS
from app.models import Deployment, DeploymentService
from app.secrets import SecretStore, SecretStoreError

log = logging.getLogger(__name__)

#: The one `deployment_service.service_key` E3 reads or writes. E5 adds the
#: rest of the services to that table; this module stays on the broker row.
MQTT_SERVICE_KEY = "mqtt"

#: Seconds between platform PINGREQs. Well under the 300s pending-revision
#: window (phase-3 fixed choice) so a dead connection is noticed and retried
#: long before a revision could time out because nobody was listening.
KEEPALIVE_SECONDS = 30


class BrokerUnavailable(RuntimeError):
    """A publish was attempted for a deployment with no live connection.

    Callers decide what that means: E3.4 leaves the revision in `draft` and
    surfaces the failure rather than pretending it published.
    """


# --- Where a connection's details come from ---------------------------------


@dataclass(frozen=True)
class BrokerCoordinates:
    """Everything needed to dial one deployment's broker.

    `slug` rides along because it is the `{dep}` topic segment (D36) — the
    manager needs it to build this deployment's subscription filters, and no
    caller should have to re-query the deployment to supply them.
    """

    deployment_id: uuid.UUID
    slug: str
    host: str
    port: int
    username: str
    #: Never logged, never repr'd (rule R2); see the module docstring.
    password: str = field(repr=False)
    tls_enabled: bool = True
    ca_cert_pem: str | None = None

    def __str__(self) -> str:
        return f"{self.slug} broker at {self.host}:{self.port}"


CoordinatesLoader = Callable[[], Sequence[BrokerCoordinates]]


def load_broker_coordinates(
    session_factory: sessionmaker[Session],
    secret_store: SecretStore,
) -> list[BrokerCoordinates]:
    """Read every deployment's MQTT service row, resolving passwords through
    SecretStore. Synchronous on purpose — the app's SQLAlchemy session is
    synchronous, and `MqttClientManager.start` calls this in a worker thread.

    A row whose secret is missing is SKIPPED with a warning rather than
    raising: one deployment provisioned badly must not stop the control plane
    for every other deployment. The warning names the secret, never its value.
    """
    coordinates: list[BrokerCoordinates] = []
    with session_factory() as db:
        rows = db.execute(
            select(DeploymentService, Deployment.slug)
            .join(Deployment, Deployment.id == DeploymentService.deployment_id)
            .where(DeploymentService.service_key == MQTT_SERVICE_KEY)
            .order_by(Deployment.slug)
        ).all()
        for service, slug in rows:
            try:
                password = secret_store.get(service.password_secret_name)
            except SecretStoreError as error:
                log.warning(
                    "skipping the %s broker: %s is unreadable (%s)",
                    slug,
                    service.password_secret_name,
                    error,
                )
                continue
            coordinates.append(
                BrokerCoordinates(
                    deployment_id=service.deployment_id,
                    slug=slug,
                    host=service.host,
                    port=service.port,
                    username=service.username,
                    password=password,
                    tls_enabled=service.tls_enabled,
                    ca_cert_pem=service.ca_cert_pem,
                )
            )
    return coordinates


def tls_context(coordinates: BrokerCoordinates) -> ssl.SSLContext | None:
    """The TLS context for one broker, or None when the row disables TLS.

    When the row carries a CA certificate the context trusts THAT CA AND
    NOTHING ELSE — not the system trust store as well. A deployment's broker
    is identified by its own private CA (spec 7.1); widening the anchor set to
    every public root would mean any certificate from any public CA for the
    broker's hostname also verified, which is a strictly weaker check than the
    one the stored PEM was put there to make. Hostname verification stays on
    in both branches: `tls_insecure` exists in aiomqtt and is never used here.
    """
    if not coordinates.tls_enabled:
        return None
    if coordinates.ca_cert_pem is None:
        # No pinned anchor: fall back to the public trust store, which is what
        # a managed broker with a Let's Encrypt certificate needs (E5).
        context = ssl.create_default_context()
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cadata=coordinates.ca_cert_pem)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


# --- Reconnect pacing -------------------------------------------------------


@dataclass(frozen=True)
class Backoff:
    """Exponential backoff with symmetric jitter, as a pure function of the
    attempt number so it can be tested without sleeping.

    Jitter is not decoration: a deployment's Aggregators and the platform all
    reconnect the instant a restarted broker accepts again, and an unjittered
    schedule has them arrive in the same millisecond on every retry.
    """

    initial: float = 1.0
    maximum: float = 60.0
    factor: float = 2.0
    jitter: float = 0.25

    def delay(self, attempt: int, *, rng: Callable[[], float] = random.random) -> float:
        """Seconds to wait before retry number `attempt` (1 = the first)."""
        if attempt < 1:
            raise ValueError(f"attempt numbering starts at 1, got {attempt}")
        base = min(self.maximum, self.initial * self.factor ** (attempt - 1))
        spread = 1.0 - self.jitter + 2.0 * self.jitter * rng()
        return min(self.maximum, base * spread)


# --- What handlers receive --------------------------------------------------


@dataclass(frozen=True)
class InboundMessage:
    """One delivered message, plus the deployment identity the topic string
    alone cannot supply. Payloads stay RAW BYTES here: parsing them into the
    spec 7.3 models is `app.contracts.mqtt`'s job, and a payload this manager
    could not parse is a message it must still deliver to a handler that can
    decide what to do about it (E3.5)."""

    deployment_id: uuid.UUID
    deployment_slug: str
    topic: str
    payload: bytes
    qos: int
    retain: bool


MessageHandler = Callable[[InboundMessage], Awaitable[None]]

#: Given a deployment slug, the topic filters a handler wants on that
#: deployment. `app.contracts.mqtt.deployment_subscriptions` has exactly this
#: shape, which is how E3.5 registers the whole device-to-platform set.
TopicFilters = Callable[[str], Sequence[str]]


@dataclass(frozen=True)
class _Registration:
    filters: TopicFilters
    handler: MessageHandler


# --- The manager ------------------------------------------------------------


class MqttClientManager:
    """One connection per deployment broker, with reconnect and resubscribe.

    Usage:

        manager = MqttClientManager(lambda: load_broker_coordinates(f, store))
        manager.subscribe(deployment_subscriptions, on_message)
        async with manager:
            ...

    `start()` loads coordinates once. Adding or removing a deployment's broker
    row therefore takes a restart of the manager, which is honest for E3.2:
    the reconciliation worker (E3.7) owns the lifecycle and can restart it.
    """

    def __init__(
        self,
        loader: CoordinatesLoader,
        *,
        backoff: Backoff | None = None,
        client_id_prefix: str = "eoe-platform",
        keepalive: int = KEEPALIVE_SECONDS,
    ) -> None:
        self._loader = loader
        self._backoff = backoff if backoff is not None else Backoff()
        self._client_id_prefix = client_id_prefix
        self._keepalive = keepalive
        # One suffix per manager instance, not per connection attempt: a
        # reconnecting client that keeps its identifier lets the broker retire
        # the old session instead of accumulating ghosts, while two API
        # replicas still never collide.
        self._instance_id = uuid.uuid4().hex[:8]
        self._registrations: list[_Registration] = []
        self._coordinates: dict[uuid.UUID, BrokerCoordinates] = {}
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._clients: dict[uuid.UUID, aiomqtt.Client] = {}
        self._connected: dict[uuid.UUID, asyncio.Event] = {}
        self._stopping = asyncio.Event()
        self._running = False

    # -- registration --

    def subscribe(self, filters: TopicFilters, handler: MessageHandler) -> None:
        """Register a handler for the filters `filters(slug)` yields.

        Only before `start()`. A registration accepted mid-flight would apply
        to connections that happen to be down at that moment only after they
        next reconnected, so some deployments would silently deliver to it and
        others would not — a bug that shows up as missing messages days later.
        """
        if self._running:
            raise RuntimeError("register subscriptions before start(); the set is fixed after that")
        self._registrations.append(_Registration(filters=filters, handler=handler))

    # -- lifecycle --

    async def start(self) -> None:
        if self._running:
            raise RuntimeError("MqttClientManager is already started")
        self._stopping.clear()
        self._running = True
        # The loader is synchronous SQLAlchemy; keep it off the event loop.
        coordinates = await asyncio.to_thread(self._loader)
        for coords in coordinates:
            self._coordinates[coords.deployment_id] = coords
            self._connected[coords.deployment_id] = asyncio.Event()
            self._tasks[coords.deployment_id] = asyncio.create_task(
                self._connection_loop(coords), name=f"mqtt-{coords.slug}"
            )
        log.info("mqtt client manager started for %d deployment(s)", len(self._tasks))

    async def stop(self) -> None:
        """Stop every connection and wait for the tasks to finish.

        Idempotent, and safe to call after a failed start: the gate runs this
        in fixture teardown, where a leaked task would poison later tests with
        'Task was destroyed but it is pending'.
        """
        self._stopping.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._clients.clear()
        self._connected.clear()
        self._coordinates.clear()
        self._running = False

    async def __aenter__(self) -> "MqttClientManager":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # -- introspection --

    @property
    def deployment_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(self._coordinates)

    def is_connected(self, deployment_id: uuid.UUID) -> bool:
        event = self._connected.get(deployment_id)
        return event is not None and event.is_set()

    async def wait_connected(self, deployment_id: uuid.UUID, timeout: float = 30.0) -> None:
        """Block until this deployment's connection is live and subscribed.

        Callers that need to know a message will be seen — tests above all —
        use this instead of sleeping, so a slow container cannot make the gate
        flaky in one direction or slow in the other.
        """
        event = self._connected.get(deployment_id)
        if event is None:
            raise BrokerUnavailable(f"no broker connection is managed for {deployment_id}")
        await asyncio.wait_for(event.wait(), timeout)

    # -- publishing --

    async def publish(
        self,
        deployment_id: uuid.UUID,
        topic: str,
        payload: bytes,
        *,
        qos: int = QOS,
        retain: bool = False,
    ) -> None:
        """Publish on this deployment's connection.

        Thin on purpose: WHICH topic, WHICH retain flag and what the bytes
        mean belong to E3.4 (desired config) and E3.10 (commands). What lives
        here is only the connection the publish rides on.
        """
        client = self._clients.get(deployment_id)
        if client is None:
            raise BrokerUnavailable(
                f"no live broker connection for deployment {deployment_id}; not publishing {topic}"
            )
        await client.publish(topic, payload, qos=qos, retain=retain)

    # -- the loop that makes outages invisible --

    async def _connection_loop(self, coords: BrokerCoordinates) -> None:
        connected_event = self._connected[coords.deployment_id]
        attempt = 0
        while not self._stopping.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=coords.host,
                    port=coords.port,
                    username=coords.username,
                    password=coords.password,
                    identifier=f"{self._client_id_prefix}-{coords.slug}-{self._instance_id}",
                    tls_context=tls_context(coords),
                    keepalive=self._keepalive,
                    clean_session=True,
                ) as client:
                    attempt = 0
                    # Subscribe BEFORE announcing the connection: a caller that
                    # publishes the moment wait_connected returns must not race
                    # its own subscription.
                    await self._subscribe_all(client, coords)
                    self._clients[coords.deployment_id] = client
                    connected_event.set()
                    log.info("connected to the %s", coords)
                    async for message in client.messages:
                        await self._dispatch(coords, message)
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as error:
                log.warning("lost the %s: %s", coords, error)
            except Exception:
                # Anything else (a TLS failure, a DNS failure) is still just a
                # broker that cannot be reached; retry rather than die and
                # leave one deployment permanently deaf.
                log.exception("could not reach the %s", coords)
            finally:
                self._clients.pop(coords.deployment_id, None)
                connected_event.clear()

            if self._stopping.is_set():
                return
            attempt += 1
            delay = self._backoff.delay(attempt)
            log.info("reconnecting to the %s in %.1fs (attempt %d)", coords, delay, attempt)
            # Sleep, but wake immediately on stop() so shutdown never waits
            # out a full backoff window.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)

    async def _subscribe_all(self, client: aiomqtt.Client, coords: BrokerCoordinates) -> None:
        for topic_filter in self._filters_for(coords.slug):
            await client.subscribe(topic_filter, qos=QOS)
        log.debug("subscribed on the %s: %s", coords, self._filters_for(coords.slug))

    def _filters_for(self, slug: str) -> tuple[str, ...]:
        """Every registered filter for one deployment, de-duplicated and in
        registration order (two handlers may legitimately want the same
        filter; the broker only needs the subscription once)."""
        seen: dict[str, None] = {}
        for registration in self._registrations:
            for topic_filter in registration.filters(slug):
                seen.setdefault(topic_filter, None)
        return tuple(seen)

    async def _dispatch(self, coords: BrokerCoordinates, message: aiomqtt.Message) -> None:
        inbound = InboundMessage(
            deployment_id=coords.deployment_id,
            deployment_slug=coords.slug,
            topic=message.topic.value,
            payload=bytes(message.payload or b""),
            qos=message.qos,
            retain=message.retain,
        )
        for registration in self._registrations:
            if not any(message.topic.matches(f) for f in registration.filters(coords.slug)):
                continue
            try:
                await registration.handler(inbound)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Property 2 in the module docstring: one device's bad payload
                # cannot cost a whole deployment its control plane.
                log.exception("message handler failed for %s on the %s", inbound.topic, coords)
