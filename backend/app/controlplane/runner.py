"""The reconciliation worker (task E3.7; spec 6.4, 6.2, 14.3).

The spec 6.4 loop, assembled. E3.2 dials the brokers, E3.4 publishes desired
config, E3.5 consumes what devices say back and E3.6 owns the transitions;
this module is the process that runs them and the two things none of them
could do, because both are about time passing rather than about a message
arriving:

* **The timeout sweep** (spec 6.4 item 4). A `pending` revision whose window
  has elapsed is failed as `timeout`. Under D70 that trigger means silence
  and nothing else — a device that answered with the wrong config already
  failed as `report_error` the moment it spoke.
* **The drift sweep** (spec 6.4 item 5). Applied devices are re-compared
  "even without a device-initiated report", which is exactly the case E3.5
  cannot cover: a device that quietly reverts its config and then reports
  with no `applied_revision_id` moves no revision, but its stored state is
  the evidence, and this sweep reads it.

**Nothing here republishes.** Spec 6.2 names an auto-reconcile policy as the
second driver of `drifted -> pending`; spec 17 item 3 has not decided what
that policy should be, so `deployment.auto_reconcile` is stored, defaults off
and is INERT. The worker reports it and never acts on it. Drift is repaired
by an operator: `POST /revisions/{id}/publish`, or E2's apply once E3.13
wires the call-through.

Three properties are load-bearing:

1. **All state lives in Postgres and in retained messages, never in the
   worker** (spec 14.3, and the phase's restart acceptance). The timeout
   window is measured from `config_revision.published_at`, written by the
   E3.6 machine; the drift comparison reads `device_state`. A worker that
   starts fresh sees exactly what the one it replaced saw, and two workers
   racing the same revision are serialized by `load_for_transition`'s row
   lock — the second one re-reads the state the first wrote and its `check`
   refuses.
2. **One transaction per revision, not per sweep.** A sweep that failed
   whole because of one bad row would leave the rest of the fleet
   unreconciled, and a sweep that committed once at the end would hold locks
   across the whole scan.
3. **The drift sweep uses E2's own snapshot composition** (`snapshot_from_raw`),
   not a local copy of the rule. A second copy would report drift on every
   Listener carrying a service key — the drift detector itself drifting.

Two entrypoints, one module (D59): `python -m app.controlplane.runner` for the
`worker` container, and `ReconciliationWorker` started in the API's lifespan
when `EOE_WORKER_IN_API` is on. Spec 3.1 draws Workers as a separate box and
production runs it that way; forcing a second container into every
integration test buys nothing but minutes.
"""

import asyncio
import contextlib
import logging
import signal
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audit import record_audit
from app.config.canonical import config_checksum
from app.config.plan import snapshot_from_raw
from app.config.service import effective_raw
from app.controlplane.broker import (
    InboundMessage,
    MqttClientManager,
    load_broker_coordinates,
)
from app.controlplane.consumer import ReportedConsumer, ReportOutcome, differing_keys, latest_state
from app.controlplane.revision_state import (
    RevisionState,
    Trigger,
    load_for_transition,
    parse_state,
    transition,
)
from app.db import create_session_factory
from app.models import (
    DEFAULT_PENDING_TIMEOUT_SECONDS,
    ConfigRevision,
    Deployment,
    utcnow,
)
from app.secrets import SecretStore
from app.services.config_sweep import ServiceConfigSweepReport, service_config_sweep
from app.services.credentials import (
    RevocationSweepReport,
    default_provider,
    drain_pending_revocations,
)
from app.settings import Settings

log = logging.getLogger(__name__)

#: The audit action for a revision the WINDOW closed on. Distinct from
#: `revision.publish` (E3.4) and `revision.report` (E3.5) because the E3.11
#: timeline has to be able to say who or what moved a revision, and "nobody
#: answered" is the one entry with no message behind it.
AUDIT_TIMEOUT = "revision.timeout"

#: The audit action for drift the sweep found rather than a report announced.
AUDIT_DRIFT = "revision.drift"


# --- What one sweep did -----------------------------------------------------


@dataclass(frozen=True)
class TimeoutSweepReport:
    """Outcome of one pass of the spec 6.4 item 4 sweep."""

    #: Revisions moved `pending -> failed(timeout)`.
    failed: tuple[uuid.UUID, ...] = ()
    #: Revisions whose window had elapsed but which were no longer `pending`
    #: when the sweep took their lock — a device's ack landed first. Not an
    #: error: it is the race the row lock exists to resolve, counted so a
    #: reader can tell "nothing was due" from "something was, and won".
    overtaken: int = 0
    #: `pending` rows with no `published_at`. Unreachable through the state
    #: machine (it stamps the column on every edge into `pending`); a
    #: hand-edited row is the only way here, and guessing a publication
    #: instant for one would fail a revision on an invented clock.
    unanchored: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.failed)


@dataclass(frozen=True)
class DriftSweepReport:
    """Outcome of one pass of the spec 6.4 item 5 sweep."""

    #: Revisions moved `applied -> drifted(report_diverged)`, because the
    #: device's stored reported state no longer matches what it applied.
    drifted: tuple[uuid.UUID, ...] = ()
    #: Devices whose recomputed effective config no longer matches the
    #: revision they applied. This is the platform's side of the comparison,
    #: and it is an OBSERVATION, not a transition: spec 6.2 has no state for
    #: "desired moved on", the config is not what the device did wrong, and
    #: creating the revision that would close the gap belongs to E2's apply.
    #: Surfaced in the log and here; nothing else acts on it.
    desired_changed: tuple[uuid.UUID, ...] = ()
    #: Applied revisions whose device has left inventory, so nothing can be
    #: recomputed for them (`target_id` is un-FK'd, D33).
    unresolvable: int = 0
    #: Applied revisions whose device has never reported, or whose reported
    #: state was deleted with the device. Nothing to compare.
    unreported: int = 0
    #: Deployments carrying drift whose `auto_reconcile` flag is on. Counted
    #: ONLY so the inert flag is visible; see the module docstring.
    auto_reconcile_requested: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.drifted)


# --- Sweep 1: the window closes (spec 6.4 item 4) ---------------------------


def pending_timeout_sweep(
    session_factory: sessionmaker[Session], *, now: datetime | None = None
) -> TimeoutSweepReport:
    """Fail every `pending` revision whose deployment's window has elapsed.

    The window comes from `deployment.pending_timeout_seconds` and falls back
    to the phase default when the deployment row is gone — a revision outlives
    its deployment by design (D33), and one that does must still resolve
    rather than sit `pending` forever.

    Selection and transition are deliberately two steps: the scan reads
    without locking, and each revision is then re-read under
    `load_for_transition` and re-checked. Between the two an ack can land, and
    the re-check is what makes the device's answer win over the clock.
    """
    at = now if now is not None else utcnow()
    with session_factory() as db:
        due = [
            (revision_id, published_at, window)
            for revision_id, published_at, window in db.execute(
                select(
                    ConfigRevision.id,
                    ConfigRevision.published_at,
                    Deployment.pending_timeout_seconds,
                )
                .outerjoin(Deployment, Deployment.id == ConfigRevision.deployment_id)
                .where(ConfigRevision.state == RevisionState.PENDING.value)
            ).all()
        ]

    unanchored = sum(1 for _, published_at, _ in due if published_at is None)
    for revision_id, published_at, _ in due:
        if published_at is None:
            log.warning(
                "revision %s is pending with no published_at; not timing it out on a guessed "
                "clock (the state machine stamps this column on every edge into pending)",
                revision_id,
            )

    expired = [
        revision_id
        for revision_id, published_at, window in due
        if published_at is not None
        and at - published_at
        >= timedelta(seconds=window if window is not None else DEFAULT_PENDING_TIMEOUT_SECONDS)
    ]

    failed: list[uuid.UUID] = []
    overtaken = 0
    for revision_id in expired:
        with session_factory() as db:
            try:
                revision = load_for_transition(db, revision_id)
                if revision is None or parse_state(revision.state) is not RevisionState.PENDING:
                    overtaken += 1
                    db.rollback()
                    continue
                waited = at - (revision.published_at or at)
                record = transition(
                    db,
                    revision,
                    RevisionState.FAILED,
                    Trigger.TIMEOUT,
                    detail={"waited_seconds": round(waited.total_seconds())},
                )
                record_audit(
                    db,
                    action=AUDIT_TIMEOUT,
                    entity_type="config_revision",
                    entity_id=str(revision.id),
                    # System-originated: no user did this, and no device did
                    # either — that is the whole content of the entry.
                    actor_user_id=None,
                    scope=revision.deployment_id,
                    detail={
                        "target_type": revision.target_type,
                        "target_id": revision.target_id,
                        "from_state": record.source.value,
                        "to_state": record.target_state.value,
                        "trigger": record.trigger.value,
                        "published_at": (
                            revision.published_at.isoformat() if revision.published_at else None
                        ),
                        "waited_seconds": round(waited.total_seconds()),
                    },
                )
                db.commit()
                failed.append(revision_id)
                log.info(
                    "revision %s timed out after %ds with no device report",
                    revision_id,
                    round(waited.total_seconds()),
                )
            except Exception:
                db.rollback()
                raise

    return TimeoutSweepReport(failed=tuple(failed), overtaken=overtaken, unanchored=unanchored)


# --- Sweep 2: drift without a report (spec 6.4 item 5) ----------------------


def desired_snapshot(db: Session, target_type: str, target_id: str) -> dict[str, Any] | None:
    """A device's effective config recomputed NOW, in publishable form.

    The same composition E2's apply uses (`snapshot_from_raw`), so a checksum
    computed from this is comparable with `config_revision.checksum` — that
    comparability is the whole point. None when the device has left inventory.
    """
    try:
        raw = effective_raw(db, target_type, target_id)
    except LookupError:
        return None
    return snapshot_from_raw(target_type, raw)


def drift_sweep(session_factory: sessionmaker[Session]) -> DriftSweepReport:
    """Re-compare every `applied` revision against reality, both directions.

    Direction one, the spec 6.2 transition: the device's stored reported state
    against the revision it applied. A mismatch is `drifted`, exactly as a
    diverging report would have been — this sweep exists for the divergences
    that arrive without one (a report naming no revision, or naming a pruned
    one, both of which E3.5 stores and deliberately does not act on).

    Direction two, an observation only: the platform's recomputed effective
    config against that same revision. When they differ, desired has moved on
    and no revision carries the change yet. That is not the device's fault and
    not a spec 6.2 state; it is logged and counted so an operator can be told
    "this device needs a re-apply", and the worker does nothing else with it.
    """
    with session_factory() as db:
        applied = [
            (row.id, row.target_type, row.target_id, row.checksum, row.deployment_id)
            for row in db.scalars(
                select(ConfigRevision)
                .where(ConfigRevision.state == RevisionState.APPLIED.value)
                .order_by(ConfigRevision.created_at, ConfigRevision.id)
            ).all()
        ]

    drifted: list[uuid.UUID] = []
    desired_changed: list[uuid.UUID] = []
    unresolvable = 0
    unreported = 0
    auto_reconcile_requested = 0

    for revision_id, target_type, target_id, checksum, deployment_id in applied:
        with session_factory() as db:
            try:
                snapshot = desired_snapshot(db, target_type, target_id)
                if snapshot is None:
                    unresolvable += 1
                    log.debug(
                        "applied revision %s targets %s %s, which is no longer in inventory",
                        revision_id,
                        target_type,
                        target_id,
                    )
                elif config_checksum(snapshot) != checksum:
                    desired_changed.append(revision_id)
                    log.info(
                        "%s %s applied revision %s, but its effective config has changed since; "
                        "an operator apply is what publishes the difference (no state change)",
                        target_type,
                        target_id,
                        revision_id,
                    )

                stored = latest_state(db, target_type, target_id)
                if stored is None:
                    unreported += 1
                    db.rollback()
                    continue
                if stored.checksum == checksum:
                    db.rollback()
                    continue

                revision = load_for_transition(db, revision_id)
                if revision is None or parse_state(revision.state) is not RevisionState.APPLIED:
                    # A report or a publish moved it while we were comparing.
                    db.rollback()
                    continue
                keys = differing_keys(stored.config, revision.snapshot)
                detail: dict[str, Any] = {
                    "differing_keys": keys,
                    "reported_at": stored.reported_at.isoformat(),
                    "reported_checksum": stored.checksum,
                    "found_by": "drift_sweep",
                }
                record = transition(
                    db,
                    revision,
                    RevisionState.DRIFTED,
                    Trigger.REPORT_DIVERGED,
                    detail=detail,
                )
                record_audit(
                    db,
                    action=AUDIT_DRIFT,
                    entity_type="config_revision",
                    entity_id=str(revision.id),
                    actor_user_id=None,
                    scope=deployment_id,
                    detail={
                        "target_type": target_type,
                        "target_id": target_id,
                        "from_state": record.source.value,
                        "to_state": record.target_state.value,
                        "trigger": record.trigger.value,
                        **detail,
                    },
                )
                if _auto_reconcile_on(db, deployment_id):
                    auto_reconcile_requested += 1
                    log.info(
                        "deployment %s has auto_reconcile on; it is INERT pending spec 17 item 3, "
                        "so revision %s waits for an operator to re-publish",
                        deployment_id,
                        revision_id,
                    )
                db.commit()
                drifted.append(revision_id)
                log.warning(
                    "%s %s has drifted from revision %s (%d differing key(s))",
                    target_type,
                    target_id,
                    revision_id,
                    len(keys),
                )
            except Exception:
                db.rollback()
                raise

    return DriftSweepReport(
        drifted=tuple(drifted),
        desired_changed=tuple(desired_changed),
        unresolvable=unresolvable,
        unreported=unreported,
        auto_reconcile_requested=auto_reconcile_requested,
    )


def _auto_reconcile_on(db: Session, deployment_id: uuid.UUID) -> bool:
    """Whether this deployment asked for auto-reconcile. READ ONLY, and read
    by nothing that decides an action — see the module docstring."""
    return bool(
        db.scalars(select(Deployment.auto_reconcile).where(Deployment.id == deployment_id)).first()
    )


# --- The process ------------------------------------------------------------


@dataclass
class WorkerCounters:
    """What this worker has seen since it started. Diagnostics, not state:
    everything that matters is in Postgres (property 1)."""

    messages: Counter[str] = field(default_factory=Counter)
    timeout_sweeps: int = 0
    drift_sweeps: int = 0
    sweep_failures: int = 0


class ReconciliationWorker:
    """The spec 6.4 loop as one runnable object.

    Usage, either entrypoint:

        worker = ReconciliationWorker(session_factory, secret_store)
        async with worker:
            await worker.wait_closed()      # or just live alongside the API

    Owns an `MqttClientManager` with E3.5's consumer registered on it, plus
    the two sweep loops. Stopping is clean and idempotent: the gate runs this
    in fixture teardown, where a leaked task poisons later tests.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        secret_store: SecretStore,
        *,
        timeout_interval: float = 30.0,
        drift_interval: float = 300.0,
        service_config_interval: float = 60.0,
        broker_refresh_interval: float = 30.0,
        manager: MqttClientManager | None = None,
        consumer: ReportedConsumer | None = None,
        heartbeat_path: Path | None = None,
        heartbeat_interval: float = 5.0,
    ) -> None:
        self._sessions = session_factory
        self._secrets = secret_store
        self._timeout_interval = timeout_interval
        self._drift_interval = drift_interval
        self._service_config_interval = service_config_interval
        self._broker_refresh_interval = broker_refresh_interval
        self._heartbeat_path = heartbeat_path
        self._heartbeat_interval = heartbeat_interval
        self._consumer = consumer if consumer is not None else ReportedConsumer(session_factory)
        self._manager = (
            manager
            if manager is not None
            else MqttClientManager(lambda: load_broker_coordinates(session_factory, secret_store))
        )
        self._tasks: list[asyncio.Task[None]] = []
        self._sweeps: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._running = False
        self.counters = WorkerCounters()

    @property
    def manager(self) -> MqttClientManager:
        """The live client manager — E3.13 and the API publish through it."""
        return self._manager

    async def start(self) -> None:
        if self._running:
            raise RuntimeError("ReconciliationWorker is already started")
        self._stopping.clear()
        # Registration must precede `start()` (the E3.2 contract), which is
        # also why the consumer is constructed in __init__ rather than here.
        self._manager.subscribe(self._consumer.filters, self._on_message)
        self._tasks = [
            asyncio.create_task(
                self._sweep_loop("timeout", self._timeout_interval, self._timeout_sweep),
                name="reconcile-timeout-sweep",
            ),
            asyncio.create_task(
                self._sweep_loop("drift", self._drift_interval, self._drift_sweep),
                name="reconcile-drift-sweep",
            ),
            # E5.7b, spec 16.4: an Aggregator created AFTER the operator saved
            # their services must still receive them. The sweep's body is
            # E5-owned (`app/services/config_sweep.py`); this file only
            # registers it, which keeps the authorized E3 diff to a
            # registration. It is a no-op over an up-to-date fleet by
            # construction — E5.7a's `changed_keys` fix is what makes that true.
            asyncio.create_task(
                self._async_sweep_loop(
                    "service-config", self._service_config_interval, self._service_config_sweep
                ),
                name="reconcile-service-config-sweep",
            ),
            # E5.6, D133: a credential whose revocation could not reach the
            # broker is retried here until it does. `revoke_pending` is a
            # promise the platform made when it let an operator delete a device
            # during a broker outage, and this loop is what keeps it.
            asyncio.create_task(
                self._async_sweep_loop(
                    "broker-credential",
                    self._service_config_interval,
                    self._broker_credential_sweep,
                ),
                name="reconcile-broker-credential-sweep",
            ),
        ]
        #: The sweeps alone — what the heartbeat vouches for. Kept apart from
        #: `_tasks` so that adding a task here never silently widens or
        #: narrows what "healthy" means.
        self._sweeps = list(self._tasks)
        # E5.7b: the coordinates poll. NOT a sweep, and deliberately outside
        # `_sweeps` — the heartbeat vouches for the sweeps, and a worker whose
        # broker-row poll died is still reconciling every deployment it already
        # holds. Widening "healthy" to include this would make a new
        # deployment's absence look like a dead worker.
        self._tasks.append(
            asyncio.create_task(self._refresh_loop(), name="mqtt-coordinates-refresh")
        )
        if self._heartbeat_path is not None:
            self._tasks.append(
                asyncio.create_task(self._heartbeat_loop(), name="reconcile-heartbeat")
            )
        # The FIRST attempt is awaited so a healthy start is fully started when
        # this returns and a caller may `wait_connected` immediately. A failed
        # one retries inside the manager (E3.2) rather than killing the
        # process: this container comes up beside Postgres, so on a first
        # `compose up` it can easily reach the coordinates query before the
        # migrations have created the table to query. A worker that exited
        # there would need a restart policy to do what waiting does.
        await self._manager.start_or_retry()
        self._running = True
        log.info(
            "reconciliation worker started (timeout sweep %.0fs, drift sweep %.0fs)",
            self._timeout_interval,
            self._drift_interval,
        )

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self._manager.stop()
        self._running = False
        log.info("reconciliation worker stopped")

    async def __aenter__(self) -> "ReconciliationWorker":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def wait_closed(self) -> None:
        """Block until `stop()` (or a signal) asks the worker to finish."""
        await self._stopping.wait()

    async def _on_message(self, message: InboundMessage) -> None:
        """The consumer's handler, wrapped only to count what it decided.

        The decision itself, and every write behind it, is E3.5's; this
        wrapper must never grow logic. Exceptions propagate to the manager,
        which logs them and keeps reading (the E3.2 contract).
        """
        outcome: ReportOutcome = await self._consumer.handle(message)
        self.counters.messages[outcome.value] += 1

    def _timeout_sweep(self) -> TimeoutSweepReport:
        report = pending_timeout_sweep(self._sessions)
        self.counters.timeout_sweeps += 1
        return report

    def _drift_sweep(self) -> DriftSweepReport:
        report = drift_sweep(self._sessions)
        self.counters.drift_sweeps += 1
        return report

    async def _sweep_loop(
        self,
        name: str,
        interval: float,
        sweep: Callable[[], TimeoutSweepReport | DriftSweepReport],
    ) -> None:
        """Run one sweep on a fixed cadence until stop.

        Sweeps run FIRST and wait afterwards: a worker that has just replaced
        one that died should resolve whatever fell overdue in the gap
        immediately, not one interval later.

        A failing sweep is logged and retried on the next tick. The
        alternative — letting the task die — would leave a worker that still
        holds its broker connection and silently stops timing anything out,
        which looks exactly like a healthy fleet.
        """
        while not self._stopping.is_set():
            try:
                report = await asyncio.to_thread(sweep)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.counters.sweep_failures += 1
                log.exception("the %s sweep failed; the loop continues", name)
            else:
                if report.changed:
                    log.info("%s sweep: %s", name, report)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), interval)

    async def _service_config_sweep(self) -> ServiceConfigSweepReport:
        return await service_config_sweep(
            self._sessions,
            self._secrets,
            self._manager,
            # The worker holds real connections, so its publishes are real.
            # `publish_all` refuses on a None publisher, which is the other
            # branch — this one is simply "the manager exists".
            publish_enabled=True,
        )

    async def _broker_credential_sweep(self) -> RevocationSweepReport:
        return await drain_pending_revocations(self._sessions, self._secrets, default_provider())

    async def _async_sweep_loop(
        self,
        name: str,
        interval: float,
        sweep: Callable[[], Awaitable[ServiceConfigSweepReport | RevocationSweepReport]],
    ) -> None:
        """`_sweep_loop` for a sweep that is natively async (E5.7b).

        Two loops rather than one that inspects its callable: the sync sweeps go
        through `asyncio.to_thread` because they are blocking SQLAlchemy, and
        these do not because they await the broker. A single loop branching on
        `inspect.iscoroutinefunction` would hide that difference at the one
        point where it decides whether the event loop blocks.

        Every other rule is `_sweep_loop`'s and is repeated on purpose: run
        FIRST and wait afterwards, so a worker replacing one that died resolves
        the gap immediately; a failing sweep is logged and retried on the next
        tick rather than killing the task, because a worker that still holds
        its broker connection and has silently stopped sweeping looks exactly
        like a healthy fleet.
        """
        while not self._stopping.is_set():
            try:
                report = await sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.counters.sweep_failures += 1
                log.exception("the %s sweep failed; the loop continues", name)
            else:
                if report.changed:
                    log.info("%s sweep: %s", name, report)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), interval)

    async def _refresh_loop(self) -> None:
        """Re-read the broker rows on a cadence (E5.7b).

        **Waits FIRST**, unlike the sweeps: `start()` has just loaded the
        coordinates, so an immediate refresh would be a guaranteed no-op and a
        wasted query on every worker start.

        A failure is logged and retried. The loader touches the database and
        the database can be briefly unavailable; a poll that died on the first
        blip would leave the worker permanently unable to notice a new
        deployment, with nothing about it visible from outside.
        """
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), self._broker_refresh_interval)
            if self._stopping.is_set():
                return
            try:
                await self._manager.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("could not refresh broker coordinates; retrying next tick")

    async def _heartbeat_loop(self) -> None:
        """Touch the liveness file, but ONLY while the sweeps are still alive.

        This is a real check, not a tick that proves the process has not
        segfaulted. The failure this worker can actually suffer is a sweep
        task dying while the process stays up holding its broker connection —
        from the outside indistinguishable from a healthy fleet, which is the
        whole reason `_sweep_loop` swallows exceptions. If that ever stops
        being enough and a task does die, the file goes stale and the
        container goes unhealthy.

        Why a file rather than a port: the worker serves nothing. Opening an
        HTTP port purely to answer a healthcheck would add a listening socket,
        a framework and a route to a process whose entire job is to talk to
        Postgres and a broker.
        """
        assert self._heartbeat_path is not None
        while not self._stopping.is_set():
            if all(not task.done() for task in self._sweeps):
                try:
                    self._heartbeat_path.write_text(str(int(time.time())), encoding="utf-8")
                except OSError:
                    log.exception("could not write the heartbeat file; the loop continues")
            else:
                log.error("a sweep task has died; letting the heartbeat go stale")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), self._heartbeat_interval)


async def run_worker(settings: Settings | None = None) -> None:
    """The standalone entrypoint's body: build everything, run until signalled.

    Its own engine and session factory — this is a process, not a request, and
    sharing the API's pool across a process boundary is not a thing that
    exists.
    """
    resolved = settings if settings is not None else Settings()  # type: ignore[call-arg]
    _, session_factory = create_session_factory(resolved.database_url)
    worker = ReconciliationWorker(
        session_factory,
        SecretStore(session_factory, resolved.kek),
        timeout_interval=float(resolved.timeout_sweep_seconds),
        drift_interval=float(resolved.drift_sweep_seconds),
        service_config_interval=float(resolved.service_config_sweep_seconds),
        broker_refresh_interval=float(resolved.broker_refresh_seconds),
        # Only the standalone process writes one: it is the container the
        # compose healthcheck probes. Under EOE_WORKER_IN_API the API's own
        # HTTP healthcheck already covers the process.
        heartbeat_path=Path(resolved.worker_heartbeat_path),
    )
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signame in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            # Windows' proactor loop implements neither; the container is
            # Linux and Ctrl-C there still arrives as KeyboardInterrupt.
            loop.add_signal_handler(getattr(signal, signame), stop.set)
    async with worker:
        await stop.wait()


def main() -> None:
    """`python -m app.controlplane.runner` — the compose `worker` service.

    Installs a root handler, because a bare process has none and Python's
    last-resort handler passes WARNING and above while silently dropping the
    rest. Everything this worker does that is worth watching — started,
    connected, which revision timed out, which device drifted — is INFO, and
    without it the container's whole narrative is invisible while the process
    looks healthy.

    **This docstring used to claim the API did not need the same treatment
    ("under uvicorn the server installs the handlers"), and that was wrong**
    (D139): uvicorn attaches handlers to its own `uvicorn.*` loggers and leaves
    the ROOT logger bare, so the API dropped every `app.*` INFO line for as long
    as that sentence stood. `create_app` now calls the same helper, which is why
    it lives in `app.middleware` rather than here — one process configuring
    logging correctly and another not was the whole defect.

    `configure_logging` is separate and still called: it installs the
    request-id record factory, and only that.
    """
    from app.middleware import configure_logging, install_root_handler

    install_root_handler()
    configure_logging()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
