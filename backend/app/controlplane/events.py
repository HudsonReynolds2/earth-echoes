"""The live-update bus (task E3.12; spec 13, 3.2, 15.1; D59).

Reconciliation transitions happen in the WORKER and websockets are held by the
API, so the two need a bus between them. It is Postgres `LISTEN`/`NOTIFY`, not
Redis: spec 3.2 names Redis for fan-out but also calls it optional, spec 15.1's
simplest self-hosted deployment omits it, and E0 wrote that promise into a
readiness test. A bus that must work without Redis cannot be Redis (D59).

**Publishing rides the caller's transaction, and that is the whole design.**
`pg_notify` issued inside a transaction is delivered by Postgres only if that
transaction COMMITS. So an event announcing a state change cannot be seen by a
browser unless the change itself was durable — no ordering to arrange, no
outbox to reconcile, no window in which the UI shows a transition that was
later rolled back. `publish()` therefore takes the caller's session and never
commits.

**Delivery is best-effort by construction, and the UI must treat it that way.**
`NOTIFY` reaches whoever is listening at that moment; a browser connected one
second later hears nothing about it. That is correct for a live-update channel
and wrong for a source of truth, so every consumer refetches from the API on
reconnect rather than reconstructing state from the stream. Nothing here is
persisted: the durable record is `reconciliation_event` (E3.11), `device_state`
and `aggregator_status`.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import psycopg
from sqlalchemy import func, select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: The single Postgres channel. One channel with a typed envelope rather than
#: one channel per event kind: a listener has to hold a connection per channel,
#: and the filtering a browser needs (by deployment, by role) is finer than a
#: channel name can express anyway.
CHANNEL = "eoe_events"

#: `NOTIFY` payloads are capped at 8000 bytes by Postgres. Nothing here comes
#: close — an event names what changed and never carries a config — but a
#: future field that did would fail at runtime on the one row big enough to
#: matter, so the boundary is checked rather than trusted.
MAX_PAYLOAD = 7000


class Channel(StrEnum):
    """The channel registry, which is a subscription vocabulary rather than a
    transport detail: a client asks for the channels it wants and gets nothing
    else.

    **E7 adds `alerts` here.** The registry is deliberately open, and the
    filtering below is channel-agnostic, so that task needs no change to this
    module beyond a member and an emitter.
    """

    #: An Aggregator's LWT verdict or a Listener's spec 6.5 liveness moved.
    DEVICE_STATUS = "device_status"
    #: A `config_revision` changed state (spec 6.2).
    RECONCILIATION = "reconciliation"


@dataclass(frozen=True)
class Event:
    """One thing that happened, addressed to whoever may see it.

    `deployment_id` is not decoration: it is the ONLY thing the websocket
    layer scopes on, so an event that omitted it would be broadcast to every
    connected session regardless of their assignments. It is required for
    exactly that reason.
    """

    channel: Channel
    deployment_id: uuid.UUID
    #: What the event is about: `aggregator`, `listener`, or `revision`.
    entity_type: str
    entity_id: str
    #: Free-form and small. Never a config body, never a secret marker's
    #: neighbour — see the module docstring on what is durable instead.
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> str:
        body = asdict(self)
        body["channel"] = self.channel.value
        body["deployment_id"] = str(self.deployment_id)
        body["at"] = self.at.isoformat()
        return json.dumps(body, separators=(",", ":"), default=str)

    @staticmethod
    def from_json(raw: str) -> "Event":
        body = json.loads(raw)
        return Event(
            channel=Channel(body["channel"]),
            deployment_id=uuid.UUID(body["deployment_id"]),
            entity_type=body["entity_type"],
            entity_id=body["entity_id"],
            data=body.get("data") or {},
            at=datetime.fromisoformat(body["at"]),
        )


def publish(db: Session, event: Event) -> None:
    """Announce one event when the caller's transaction commits.

    Staged, never committed here — the `record_audit` convention. If the
    caller rolls back, Postgres discards the notification too, so a browser
    can never be told about a transition that did not happen.
    """
    payload = event.to_json()
    if len(payload) > MAX_PAYLOAD:
        # Refusing loudly beats a runtime failure on whichever event happened
        # to be the large one. Nothing in this phase can reach it.
        raise ValueError(f"event payload is {len(payload)} bytes, over the NOTIFY limit")
    db.execute(select(func.pg_notify(CHANNEL, payload)))


# --- The listening half -----------------------------------------------------


def _raw_dsn(database_url: str) -> str:
    """SQLAlchemy's URL to a libpq one. `LISTEN` needs a connection nobody
    else is using, so this deliberately does not borrow the app's pool."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


async def listen(
    database_url: str,
    on_event: Callable[[Event], None],
    *,
    stopping: asyncio.Event | None = None,
    reconnect_delay: float = 2.0,
) -> None:
    """Hold a `LISTEN` connection and hand every event to `on_event`.

    Runs for the life of the API process. A dropped connection is retried
    forever rather than being fatal: losing the bus costs live updates, and
    every consumer refetches on reconnect, so the correct response is to keep
    trying quietly and not to take the API down with it.

    A malformed payload is logged and skipped. The only writer is `publish`
    above, so one can only appear if something else is NOTIFYing on this
    channel — worth a line in the log and not worth dropping the connection.
    """
    stop = stopping if stopping is not None else asyncio.Event()
    while not stop.is_set():
        try:
            async with await psycopg.AsyncConnection.connect(
                _raw_dsn(database_url), autocommit=True
            ) as conn:
                await conn.execute(f"LISTEN {CHANNEL}")
                log.info("listening for live updates on %s", CHANNEL)
                async for notify in conn.notifies():
                    try:
                        on_event(Event.from_json(notify.payload))
                    except Exception:
                        log.exception("dropping an unreadable event payload")
                    if stop.is_set():
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the live-update listener lost its connection; retrying")
        if stop.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), reconnect_delay)


class Hub:
    """Fan-out to the websockets this process is holding.

    One per API process. Subscribers are asyncio queues rather than callbacks
    so that a slow browser cannot block the listener that feeds every other
    one — and a queue that fills is DROPPED FROM rather than awaited, for the
    same reason. Losing live updates to a stalled client is a much smaller
    problem than stalling the bus for everybody.
    """

    def __init__(self, queue_size: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size

    def dispatch(self, event: Event) -> None:
        """Called by the listener. Never blocks and never raises."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("dropping a live update for a subscriber that is not keeping up")

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def stream(self, queue: asyncio.Queue[Event]) -> AsyncIterator[Event]:
        while True:
            yield await queue.get()
