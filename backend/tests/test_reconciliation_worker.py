"""Gate 45: the reconciliation worker (task E3.7; spec 6.2, 6.4, 14.3).

The phase document sets one acceptance criterion in two halves, and both are
at the bottom of this file against a real broker and a real worker:

* `test_the_full_journey_publish_ack_drift_republish_and_timeout` drives a
  mock device through every edge the phase names, in order.
* `test_a_restarted_worker_resumes_the_windows_it_did_not_start` stops the
  worker mid-flight and lets a NEW one finish the job. Nothing is handed
  over: the window is in `config_revision.published_at` and the desired
  config is in a retained message, which is what spec 14.3 means by
  surviving a restart.

Everything above them drives the two sweeps directly. That is deliberate: a
red test up there means the comparison or the transition is wrong, rather
than a container being slow.

The suite reads states back out of the database rather than trusting a return
value, because `revision_state.transition` is the only writer of `state` and
the point of these tests is what it was asked to do.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    REPO_ROOT,
    ephemeral_broker,
    ephemeral_postgres,
    free_port,
    make_kek,
)
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import ProgrammingError
from test_auth import PASSWORD  # one login password for every suite

from app.auth.passwords import hash_password
from app.config.canonical import config_checksum
from app.config.plan import snapshot_from_raw
from app.config.service import effective_raw
from app.contracts.mqtt import (
    QOS,
    DesiredConfig,
    ReportedAggregatorState,
    decode,
    desired_topic,
    encode,
    reported_topic,
)
from app.controlplane import runner
from app.controlplane.broker import (
    BrokerUnavailable,
    MqttClientManager,
    load_broker_coordinates,
)
from app.controlplane.publisher import publish_revision
from app.controlplane.revision_state import RevisionState
from app.controlplane.runner import (
    AUDIT_DRIFT,
    AUDIT_TIMEOUT,
    ReconciliationWorker,
    desired_snapshot,
    drift_sweep,
    pending_timeout_sweep,
)
from app.db import create_session_factory
from app.devbroker import device_username, load_manifest
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AuditLog,
    ConfigRevision,
    Deployment,
    DeploymentService,
    DeviceState,
    EntityOverride,
    RoleAssignment,
    User,
    utcnow,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy
from app.settings import Settings

BACKEND = REPO_ROOT / "backend"

RC = "redwood-coast"
AGG = "demo-agg-rc-01"
#: The first Listener under AGG in the demo hierarchy (seed.DEMO_HIERARCHY).
MAC = "02:EE:0E:01:01:01"

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SNAPSHOT = {
    "capture.mode": "continuous",
    "capture.sample_rate_hz": 48000,
    "logging.verbosity": "info",
}
#: What a drifted device reports: same keys, one different value, so
#: `differing_keys` has something true to say.
DIVERGED = {**SNAPSHOT, "logging.verbosity": "debug"}


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def database():
    """One migrated Postgres carrying the demo hierarchy for the whole module."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        yield factory


@pytest.fixture
def sessions(database):
    """The session factory, with everything this suite writes reset after it.

    The deployment policy columns are reset too: a test that shortens one
    deployment's window must not shorten it for the next test, and that is
    exactly the kind of leak a shared module database invites.
    """
    yield database
    with database() as db:
        db.execute(delete(ConfigRevision))
        db.execute(delete(DeviceState))
        db.execute(delete(EntityOverride))
        db.execute(delete(AuditLog).where(AuditLog.action.in_([AUDIT_TIMEOUT, AUDIT_DRIFT])))
        db.execute(update(Deployment).values(pending_timeout_seconds=300, auto_reconcile=False))
        db.commit()


def deployment_id_of(factory, slug: str = RC) -> uuid.UUID:
    with factory() as db:
        return db.scalars(select(Deployment.id).where(Deployment.slug == slug)).one()


def platform_uuid_of(factory, aggregator_uuid: str = AGG) -> str:
    """The Aggregator's PLATFORM UUID — what `config_revision.target_id`
    carries, and NOT the `aggregator_uuid` in the topic (spec 4.2; D75)."""
    with factory() as db:
        return str(
            db.scalars(
                select(Aggregator.id).where(Aggregator.aggregator_uuid == aggregator_uuid)
            ).one()
        )


def make_revision(
    factory,
    *,
    target_type: str = "aggregator",
    target_id: str | None = None,
    state: str = "pending",
    snapshot: dict | None = None,
    published_ago: timedelta | None = timedelta(0),
    deployment_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One committed revision, with `published_at` under the test's control.

    Every timeout test is a statement about how long ago a revision entered
    `pending`, so the anchor is set explicitly rather than by publishing and
    waiting. `published_ago=None` leaves it NULL — the hand-edited row the
    sweep refuses to time out.
    """
    body = SNAPSHOT if snapshot is None else snapshot
    revision = ConfigRevision(
        target_type=target_type,
        target_id=target_id if target_id is not None else platform_uuid_of(factory),
        deployment_id=deployment_id if deployment_id is not None else deployment_id_of(factory),
        snapshot=body,
        schema_version=1,
        checksum=config_checksum(body),
        state=state,
        published_at=None if published_ago is None else utcnow() - published_ago,
    )
    with factory() as db:
        db.add(revision)
        db.commit()
        return revision.id


def store_report(
    factory,
    *,
    target_type: str = "aggregator",
    target_id: str | None = None,
    config: dict | None = None,
    revision_id: uuid.UUID | None = None,
    reported_at: datetime | None = None,
) -> None:
    """A `device_state` row as E3.5 would have written it."""
    body = SNAPSHOT if config is None else config
    with factory() as db:
        db.add(
            DeviceState(
                entity_type=target_type,
                entity_id=target_id if target_id is not None else platform_uuid_of(factory),
                deployment_id=deployment_id_of(factory),
                reported_at=reported_at or utcnow(),
                applied_revision_id=revision_id,
                checksum=config_checksum(body),
                config=body,
            )
        )
        db.commit()


def state_of(factory, revision_id: uuid.UUID) -> str:
    with factory() as db:
        return db.scalars(
            select(ConfigRevision.state).where(ConfigRevision.id == revision_id)
        ).one()


def published_at_of(factory, revision_id: uuid.UUID) -> datetime | None:
    with factory() as db:
        return db.scalars(
            select(ConfigRevision.published_at).where(ConfigRevision.id == revision_id)
        ).one()


def audit_rows(factory, action: str, revision_id: uuid.UUID) -> list[AuditLog]:
    with factory() as db:
        return list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.action == action, AuditLog.entity_id == str(revision_id)
                )
            ).all()
        )


def set_policy(factory, *, timeout: int | None = None, auto_reconcile: bool | None = None) -> None:
    values: dict[str, object] = {}
    if timeout is not None:
        values["pending_timeout_seconds"] = timeout
    if auto_reconcile is not None:
        values["auto_reconcile"] = auto_reconcile
    with factory() as db:
        db.execute(update(Deployment).where(Deployment.slug == RC).values(**values))
        db.commit()


@dataclass
class FakePublisher:
    """Satisfies `DesiredPublisher` structurally and remembers everything."""

    fails_with: Exception | None = None
    sent: list[tuple[str, bytes, bool]] = field(default_factory=list)

    async def publish(
        self,
        deployment_id: uuid.UUID,
        topic: str,
        payload: bytes,
        *,
        qos: int = QOS,
        retain: bool = False,
    ) -> None:
        if self.fails_with is not None:
            raise self.fails_with
        self.sent.append((topic, payload, retain))


# =========================================================================
# The window anchor: `published_at`, written by the state machine
# =========================================================================


async def test_publishing_stamps_the_window_the_timeout_is_measured_from(sessions):
    """`created_at` cannot serve: E2 writes it when the operator saved a draft,
    which may be days before anyone published it."""
    revision_id = make_revision(sessions, state="draft", published_ago=None)
    assert published_at_of(sessions, revision_id) is None

    await publish_revision(sessions, FakePublisher(), revision_id, publish_enabled=True)

    anchor = published_at_of(sessions, revision_id)
    assert anchor is not None
    assert abs((utcnow() - anchor).total_seconds()) < 60


async def test_every_edge_back_into_pending_restarts_the_window(sessions):
    """Spec 6.2 reaches `pending` three ways. An operator retrying a revision
    that failed an hour ago is starting a fresh wait, not inheriting an
    expired one — from the old anchor it would fail again on the next sweep
    without the device having been given a chance to answer."""
    for state in ("failed", "drifted"):
        stale_anchor = utcnow() - timedelta(hours=1)
        revision_id = make_revision(sessions, state=state, published_ago=timedelta(hours=1))
        assert published_at_of(sessions, revision_id) is not None

        await publish_revision(sessions, FakePublisher(), revision_id, publish_enabled=True)

        anchor = published_at_of(sessions, revision_id)
        assert anchor is not None
        assert anchor > stale_anchor, f"{state} -> pending kept a stale window"
        with sessions() as db:
            db.execute(delete(ConfigRevision))
            db.commit()


async def test_leaving_pending_keeps_the_anchor_as_history(sessions):
    """It records when the revision was published, not when it was pending."""
    revision_id = make_revision(sessions, state="draft", published_ago=None)
    await publish_revision(sessions, FakePublisher(), revision_id, publish_enabled=True)
    anchor = published_at_of(sessions, revision_id)

    store_report(sessions, revision_id=revision_id)
    pending_timeout_sweep(sessions, now=utcnow() + timedelta(hours=1))
    with sessions() as db:
        db.execute(
            update(ConfigRevision).where(ConfigRevision.id == revision_id).values(state="applied")
        )
        db.commit()

    assert published_at_of(sessions, revision_id) == anchor


# =========================================================================
# Sweep 1: the pending window (spec 6.4 item 4)
# =========================================================================


def test_a_pending_revision_past_its_window_fails_as_a_timeout(sessions):
    revision_id = make_revision(sessions, published_ago=timedelta(seconds=301))

    report = pending_timeout_sweep(sessions)

    assert report.failed == (revision_id,)
    assert state_of(sessions, revision_id) == RevisionState.FAILED
    (row,) = audit_rows(sessions, AUDIT_TIMEOUT, revision_id)
    assert row.actor_user_id is None, "a timeout has no actor; nobody did it"
    assert row.detail is not None
    assert row.detail["trigger"] == "timeout"
    assert row.detail["from_state"] == "pending"
    assert row.detail["waited_seconds"] >= 301


def test_a_pending_revision_inside_its_window_is_left_alone(sessions):
    revision_id = make_revision(sessions, published_ago=timedelta(seconds=299))

    report = pending_timeout_sweep(sessions)

    assert report.failed == ()
    assert state_of(sessions, revision_id) == RevisionState.PENDING
    assert audit_rows(sessions, AUDIT_TIMEOUT, revision_id) == []


def test_the_window_is_the_deployments_own(sessions):
    """ "Configurable per deployment" (phase-3 fixed choice). A deployment on a
    satellite link can wait ten minutes; one on fibre should not."""
    set_policy(sessions, timeout=60)
    revision_id = make_revision(sessions, published_ago=timedelta(seconds=90))

    report = pending_timeout_sweep(sessions)

    assert report.failed == (revision_id,)
    assert state_of(sessions, revision_id) == RevisionState.FAILED, (
        "90 seconds is inside the 300s default and well past this deployment's 60"
    )


def test_a_revision_that_outlived_its_deployment_still_resolves(sessions):
    """`config_revision.deployment_id` is un-FK'd (D33), so a revision can name
    a deployment that no longer exists. It must not sit `pending` forever
    because the row that held its window is gone."""
    revision_id = make_revision(
        sessions, published_ago=timedelta(seconds=400), deployment_id=uuid.uuid4()
    )

    report = pending_timeout_sweep(sessions)

    assert report.failed == (revision_id,)
    assert state_of(sessions, revision_id) == RevisionState.FAILED


def test_a_pending_revision_with_no_anchor_is_reported_not_failed(sessions):
    """Unreachable through the machine, which stamps the column on every edge
    into `pending`. A hand-edited row must not be failed on a guessed clock:
    `failed(timeout)` means the device stayed silent for a known window (D70),
    and there is no known window here."""
    revision_id = make_revision(sessions, published_ago=None)

    report = pending_timeout_sweep(sessions)

    assert report.unanchored == 1
    assert report.failed == ()
    assert state_of(sessions, revision_id) == RevisionState.PENDING


@pytest.mark.parametrize("state", ["draft", "applied", "drifted", "failed", "superseded"])
def test_the_sweep_touches_nothing_that_is_not_pending(sessions, state):
    """Only `pending -> failed(timeout)` is a spec 6.2 edge for the clock. A
    sweep that offered any other state to the machine would raise where
    nothing is wrong."""
    revision_id = make_revision(sessions, state=state, published_ago=timedelta(days=1))

    report = pending_timeout_sweep(sessions)

    assert report.failed == ()
    assert state_of(sessions, revision_id) == state


def test_running_the_sweep_twice_changes_nothing_the_second_time(sessions):
    revision_id = make_revision(sessions, published_ago=timedelta(seconds=400))

    first = pending_timeout_sweep(sessions)
    second = pending_timeout_sweep(sessions)

    assert first.failed == (revision_id,)
    assert second.failed == ()
    assert len(audit_rows(sessions, AUDIT_TIMEOUT, revision_id)) == 1


def test_an_ack_that_lands_first_wins_the_race(sessions, monkeypatch):
    """The scan is lockless and the transition is not: between them a device's
    report can move the revision. The re-read under the row lock is what makes
    the device's answer beat the clock, rather than the later write winning."""
    revision_id = make_revision(sessions, published_ago=timedelta(seconds=400))
    real = runner.load_for_transition

    def ack_arrives_first(db, wanted_id):
        revision = real(db, wanted_id)
        if revision is not None:
            revision.state = RevisionState.APPLIED.value  # the consumer got here first
        return revision

    monkeypatch.setattr(runner, "load_for_transition", ack_arrives_first)
    report = pending_timeout_sweep(sessions)

    assert report.failed == ()
    assert report.overtaken == 1
    assert state_of(sessions, revision_id) == RevisionState.PENDING, (
        "the simulated ack was rolled back with the sweep's own transaction"
    )


# =========================================================================
# Sweep 2: drift without a report (spec 6.4 item 5)
# =========================================================================


def test_a_device_whose_stored_state_diverges_is_drifted(sessions):
    """The case E3.5 cannot cover: the device reported config that is not what
    it applied, naming no revision, so no report-driven edge existed. The
    stored state is the evidence and this sweep is what reads it."""
    revision_id = make_revision(sessions, state="applied")
    store_report(sessions, config=DIVERGED, revision_id=None)

    report = drift_sweep(sessions)

    assert report.drifted == (revision_id,)
    assert state_of(sessions, revision_id) == RevisionState.DRIFTED
    (row,) = audit_rows(sessions, AUDIT_DRIFT, revision_id)
    assert row.detail is not None
    assert row.detail["trigger"] == "report_diverged"
    assert row.detail["found_by"] == "drift_sweep"
    assert row.detail["differing_keys"] == ["logging.verbosity"]
    assert "debug" not in str(row.detail), "the detail names keys, never values (rule R2)"


def test_a_device_reporting_what_it_applied_is_left_alone(sessions):
    revision_id = make_revision(sessions, state="applied")
    store_report(sessions, config=SNAPSHOT, revision_id=revision_id)

    report = drift_sweep(sessions)

    assert report.drifted == ()
    assert state_of(sessions, revision_id) == RevisionState.APPLIED
    assert audit_rows(sessions, AUDIT_DRIFT, revision_id) == []


def test_a_device_that_has_never_reported_is_not_drift(sessions):
    """Silence is not divergence. It is what the timeout window is for."""
    revision_id = make_revision(sessions, state="applied")

    report = drift_sweep(sessions)

    assert report.unreported == 1
    assert report.drifted == ()
    assert state_of(sessions, revision_id) == RevisionState.APPLIED


@pytest.mark.parametrize("state", ["draft", "pending", "drifted", "failed", "superseded"])
def test_only_applied_revisions_are_re_compared(sessions, state):
    """Spec 6.4 item 5 says "applied devices". A `pending` revision that does
    not match is the timeout's business, not drift's."""
    revision_id = make_revision(sessions, state=state)
    store_report(sessions, config=DIVERGED)

    report = drift_sweep(sessions)

    assert report.drifted == ()
    assert state_of(sessions, revision_id) == state


def test_drifting_twice_is_not_a_second_transition(sessions):
    revision_id = make_revision(sessions, state="applied")
    store_report(sessions, config=DIVERGED)

    first = drift_sweep(sessions)
    second = drift_sweep(sessions)

    assert first.drifted == (revision_id,)
    assert second.drifted == ()
    assert len(audit_rows(sessions, AUDIT_DRIFT, revision_id)) == 1


def test_a_revision_whose_device_is_gone_is_counted_not_crashed(sessions):
    """Un-FK'd by design (D33): revision history outlives its devices, and a
    sweep that raised on one would stop re-comparing the whole fleet."""
    revision_id = make_revision(sessions, state="applied", target_id=str(uuid.uuid4()))

    report = drift_sweep(sessions)

    assert report.unresolvable == 1
    assert state_of(sessions, revision_id) == RevisionState.APPLIED


def test_effective_config_moving_on_is_an_observation_not_a_transition(sessions):
    """The platform's side of the comparison. An override edited after the
    revision was applied means desired has moved on — which is not something
    the DEVICE did, has no spec 6.2 state, and is repaired by an apply (E2)
    rather than by this worker inventing a revision."""
    platform_uuid = platform_uuid_of(sessions)
    with sessions() as db:
        snapshot = snapshot_from_raw("aggregator", effective_raw(db, "aggregator", platform_uuid))
    revision_id = make_revision(sessions, state="applied", snapshot=snapshot)
    store_report(sessions, config=snapshot, revision_id=revision_id)

    clean = drift_sweep(sessions)
    assert clean.desired_changed == (), "a revision built from live config must not read as changed"
    assert clean.drifted == ()

    with sessions() as db:
        db.add(
            EntityOverride(
                entity_type="aggregator",
                entity_id=platform_uuid,
                overrides={"logging.verbosity": "debug"},
                catalog_version=1,
            )
        )
        db.commit()
    changed = drift_sweep(sessions)

    assert changed.desired_changed == (revision_id,)
    assert changed.drifted == (), "the device is doing exactly what it was told"
    assert state_of(sessions, revision_id) == RevisionState.APPLIED
    assert audit_rows(sessions, AUDIT_DRIFT, revision_id) == []


def test_the_detector_uses_e2s_own_snapshot_composition(sessions):
    """Listener snapshots exclude the write-restricted service keys (spec 5.4).
    A drift sweep with its own copy of that rule would report every Listener
    carrying a service key as drifted — the detector drifting, which is the
    one defect a drift detector cannot have."""
    with sessions() as db:
        listener_raw = effective_raw(db, "listener", MAC)
        snapshot = desired_snapshot(db, "listener", MAC)
    assert snapshot is not None
    restricted = {key for key, value in listener_raw.items() if key not in snapshot}
    assert restricted, "the demo catalog has no write-restricted keys; this test proves nothing"
    assert snapshot == snapshot_from_raw("listener", listener_raw)

    revision_id = make_revision(
        sessions, target_type="listener", target_id=MAC, state="applied", snapshot=snapshot
    )
    store_report(sessions, target_type="listener", target_id=MAC, config=snapshot)

    report = drift_sweep(sessions)

    assert report.desired_changed == ()
    assert report.drifted == ()
    assert state_of(sessions, revision_id) == RevisionState.APPLIED


def test_auto_reconcile_is_stored_and_does_nothing(sessions):
    """Spec 6.2 names an auto-reconcile policy as the second driver of
    `drifted -> pending`; spec 17 item 3 has not decided what it should be.
    The flag is on here and the revision still waits for an operator."""
    set_policy(sessions, auto_reconcile=True)
    revision_id = make_revision(sessions, state="applied", published_ago=timedelta(minutes=5))
    anchor = published_at_of(sessions, revision_id)
    store_report(sessions, config=DIVERGED)

    report = drift_sweep(sessions)

    assert report.auto_reconcile_requested == 1
    assert state_of(sessions, revision_id) == RevisionState.DRIFTED, (
        "an auto-republish would have driven this back to pending"
    )
    assert published_at_of(sessions, revision_id) == anchor, (
        "the window anchor moved, so something re-entered pending behind our back"
    )


# =========================================================================
# The process: wiring, cadence, and staying alive
# =========================================================================


@dataclass
class StubManager:
    """Records what the worker registered and never opens a socket."""

    registrations: list[tuple[object, object]] = field(default_factory=list)
    started: int = 0
    stopped: int = 0

    def subscribe(self, filters, handler) -> None:
        self.registrations.append((filters, handler))

    async def start(self) -> None:
        self.started += 1

    async def start_or_retry(self) -> bool:
        """The real manager's contract (D87): a failed start is REPORTED, not
        raised, so its host stays up. The double deliberately does not retry
        in the background — a real timer inside a test double is a flake
        source, and `MqttClientManager`'s own suite covers the retry loop.
        """
        try:
            await self.start()
        except Exception:
            return False
        return True

    async def stop(self) -> None:
        self.stopped += 1


async def test_the_worker_registers_the_consumer_before_connecting(sessions):
    """The E3.2 contract: the subscription set is fixed before `start()`, so a
    reconnect can never restore a partial view of the namespace. A worker that
    subscribed afterwards would deliver nothing until the first reconnect."""
    manager = StubManager()
    worker = ReconciliationWorker(
        sessions, SecretStore(sessions, make_kek()), manager=manager, timeout_interval=3600
    )
    async with worker:
        pass

    (registration,) = manager.registrations
    filters, _handler = registration
    assert filters(RC) == (
        f"eoe/{RC}/agg/+/reported",
        f"eoe/{RC}/agg/+/status",
        f"eoe/{RC}/agg/+/event",
        f"eoe/{RC}/agg/+/lst/+/reported",
    )
    assert manager.started == 1 and manager.stopped == 1


async def test_the_sweeps_run_on_their_own_without_anyone_calling_them(sessions):
    """The whole point of a worker: nobody is holding a request open."""
    revision_id = make_revision(sessions, published_ago=timedelta(seconds=400))
    worker = ReconciliationWorker(
        sessions,
        SecretStore(sessions, make_kek()),
        manager=StubManager(),
        timeout_interval=0.1,
        drift_interval=0.1,
    )
    async with worker:
        for _ in range(100):
            if state_of(sessions, revision_id) == RevisionState.FAILED:
                break
            await asyncio.sleep(0.1)

    assert state_of(sessions, revision_id) == RevisionState.FAILED
    assert worker.counters.timeout_sweeps >= 1
    assert worker.counters.drift_sweeps >= 1


async def test_a_failing_sweep_does_not_stop_the_loop(sessions, monkeypatch):
    """A worker that lost its sweep task would keep its broker connection and
    silently stop timing anything out, which looks exactly like a healthy
    fleet. It has to keep trying."""
    calls = {"n": 0}
    real = runner.pending_timeout_sweep

    def fail_once(factory, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the database blinked")
        return real(factory, **kwargs)

    monkeypatch.setattr(runner, "pending_timeout_sweep", fail_once)
    revision_id = make_revision(sessions, published_ago=timedelta(seconds=400))
    worker = ReconciliationWorker(
        sessions,
        SecretStore(sessions, make_kek()),
        manager=StubManager(),
        timeout_interval=0.1,
        drift_interval=3600,
    )
    async with worker:
        for _ in range(100):
            if state_of(sessions, revision_id) == RevisionState.FAILED:
                break
            await asyncio.sleep(0.1)

    assert worker.counters.sweep_failures == 1
    assert state_of(sessions, revision_id) == RevisionState.FAILED


async def test_a_worker_that_starts_before_its_database_does_not_die(sessions):
    """The compose `worker` comes up beside Postgres, so on a first
    `compose up` it can reach the coordinates query before the migrations have
    created the table to query. Exiting there would need a restart policy to
    do what waiting does — and a crash-looping container is not a worker."""

    @dataclass
    class RefusingManager(StubManager):
        attempts: int = 0

        async def start(self) -> None:
            self.attempts += 1
            raise RuntimeError('relation "deployment_service" does not exist')

    manager = RefusingManager()
    revision_id = make_revision(sessions, published_ago=timedelta(seconds=400))
    worker = ReconciliationWorker(
        sessions,
        SecretStore(sessions, make_kek()),
        manager=manager,
        timeout_interval=0.1,
        drift_interval=3600,
    )
    async with worker:
        for _ in range(100):
            if state_of(sessions, revision_id) == RevisionState.FAILED:
                break
            await asyncio.sleep(0.1)

    assert state_of(sessions, revision_id) == RevisionState.FAILED, (
        "the sweeps must run even with no broker: a timeout is decided by the "
        "clock and the database, and needs no connection at all"
    )
    assert manager.attempts >= 1


async def test_stopping_is_clean_and_repeatable(sessions):
    """Fixture teardown calls this; a leaked task poisons every later test."""
    manager = StubManager()
    worker = ReconciliationWorker(
        sessions, SecretStore(sessions, make_kek()), manager=manager, timeout_interval=3600
    )
    await worker.start()
    await worker.stop()
    await worker.stop()
    assert manager.stopped == 2

    # And a second start is refused while one is live, because two sweep
    # loops in one process would both take the same row locks.
    await worker.start()
    with pytest.raises(RuntimeError, match="already started"):
        await worker.start()
    await worker.stop()


# =========================================================================
# The operator's half: POST /revisions/{id}/publish
# =========================================================================

OWNER = "worker-owner@example.com"
OTHER_OP = "worker-other-op@example.com"
VIEWER = "worker-viewer@example.com"


@pytest.fixture(scope="module")
def api_app(database):
    """An app over the module database, with three actors in three scopes."""
    with database() as db:
        url = db.get_bind().url.render_as_string(hide_password=False)
        rc_id = db.scalars(select(Deployment.id).where(Deployment.slug == RC)).one()
        other_id = db.scalars(select(Deployment.id).where(Deployment.slug != RC)).first()
    app = create_app(
        Settings(
            database_url=url,
            session_secret="gate45-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    with app.state.session_factory() as db:
        for email, role, scope in (
            (OWNER, "owner", None),
            (OTHER_OP, "deployment_operator", other_id),
            (VIEWER, "viewer", rc_id),
        ):
            if db.scalars(select(User).where(User.email == email)).first() is not None:
                continue
            user = User(email=email, password_hash=hash_password(PASSWORD))
            user.role_assignments.append(RoleAssignment(role=role, deployment_id=scope))
            db.add(user)
        db.commit()
    return app


def login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def publish_call(client: TestClient, revision_id: uuid.UUID):
    return client.post(
        f"{API_PREFIX}/revisions/{revision_id}/publish",
        headers={"X-CSRF-Token": client.cookies["eoe_csrf"]},
    )


def test_the_api_starts_even_when_the_broker_rows_cannot_be_read(database, monkeypatch):
    """The defect gate 45 caught (D87), pinned so it cannot come back.

    With publishing on, the lifespan reads the `deployment_service` rows at
    startup, and in compose the API comes up beside Postgres — it can reach
    that query before the migrations have created the table. A bare `start()`
    there raises, uvicorn exits 3, and the whole API dies over a feature most
    of its routes do not use. `start_or_retry` degrades publishing instead.
    """
    with database() as db:
        url = db.get_bind().url.render_as_string(hide_password=False)
    app = create_app(
        Settings(
            database_url=url,
            session_secret="gate45-lifespan-secret",
            kek=make_kek(),
            cors_origins="",
            publish_enabled=True,
        )
    )

    def unmigrated(*args: object, **kwargs: object) -> list[object]:
        raise ProgrammingError("SELECT ... FROM deployment_service", {}, Exception("no table"))

    monkeypatch.setattr("app.main.load_broker_coordinates", unmigrated)

    with TestClient(app) as client:
        assert client.get(f"{API_PREFIX}/health").status_code == 200
        assert app.state.mqtt is not None, (
            "the manager must still be parked on app.state so the publish route "
            "answers 503 for a broker that is not up rather than 409 'disabled'"
        )


def test_publishing_an_unknown_revision_is_a_404(api_app, sessions):
    response = publish_call(login(api_app, OWNER), uuid.uuid4())
    assert response.status_code == 404


def test_a_revision_outside_the_callers_scope_is_a_404(api_app, sessions):
    """The D35 existence-oracle rule: an operator in another deployment must
    not learn that this revision exists."""
    revision_id = make_revision(sessions, state="draft", published_ago=None)
    response = publish_call(login(api_app, OTHER_OP), revision_id)
    assert response.status_code == 404
    assert state_of(sessions, revision_id) == RevisionState.DRAFT


def test_a_viewer_who_can_see_it_is_told_403_not_404(api_app, sessions):
    """They can already read this revision through GET /revisions/{id}, so a
    404 here would be a lie about a row they are looking at."""
    revision_id = make_revision(sessions, state="draft", published_ago=None)
    response = publish_call(login(api_app, VIEWER), revision_id)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert "manage_config" in response.json()["error"]["message"]


def test_publishing_requires_csrf(api_app, sessions):
    revision_id = make_revision(sessions, state="draft", published_ago=None)
    client = login(api_app, OWNER)
    response = client.post(f"{API_PREFIX}/revisions/{revision_id}/publish")
    assert response.status_code == 403


def test_publishing_refuses_while_the_flag_is_off(api_app, sessions):
    """`EOE_PUBLISH_ENABLED` defaults off until E3.13 (D61), and with it off
    the API holds no outbound connection at all."""
    revision_id = make_revision(sessions, state="draft", published_ago=None)
    response = publish_call(login(api_app, OWNER), revision_id)
    assert response.status_code == 409
    assert "EOE_PUBLISH_ENABLED" in response.json()["error"]["message"]
    assert state_of(sessions, revision_id) == RevisionState.DRAFT


def test_an_operator_publish_reaches_the_broker_and_moves_the_revision(api_app, sessions):
    revision_id = make_revision(sessions, state="draft", published_ago=None)
    publisher = FakePublisher()
    api_app.state.mqtt = publisher
    api_app.state.settings.publish_enabled = True
    try:
        response = publish_call(login(api_app, OWNER), revision_id)
    finally:
        api_app.state.mqtt = None
        api_app.state.settings.publish_enabled = False

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "pending"
    assert body["trigger"] == "publish"
    assert body["transitioned"] is True
    assert body["topic"] == desired_topic(RC, AGG)
    (topic, payload, retain) = publisher.sent[0]
    assert retain is True, "spec 7.2 marks the desired topics retained"
    assert decode(DesiredConfig, payload).revision_id == revision_id
    assert state_of(sessions, revision_id) == RevisionState.PENDING


def test_a_broker_outage_answers_503_and_publishes_nothing(api_app, sessions):
    """Retryable, and honestly so: the publish rides inside the transaction
    (D74), so the revision is exactly where it was."""
    revision_id = make_revision(sessions, state="draft", published_ago=None)
    api_app.state.mqtt = FakePublisher(fails_with=BrokerUnavailable("no live connection"))
    api_app.state.settings.publish_enabled = True
    try:
        response = publish_call(login(api_app, OWNER), revision_id)
    finally:
        api_app.state.mqtt = None
        api_app.state.settings.publish_enabled = False

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert state_of(sessions, revision_id) == RevisionState.DRAFT


def test_republishing_over_drift_is_the_operators_repair(api_app, sessions):
    """The drifted -> pending edge, which nothing else in this phase drives:
    the worker never auto-republishes."""
    revision_id = make_revision(sessions, state="drifted", published_ago=timedelta(hours=1))
    api_app.state.mqtt = FakePublisher()
    api_app.state.settings.publish_enabled = True
    try:
        response = publish_call(login(api_app, OWNER), revision_id)
    finally:
        api_app.state.mqtt = None
        api_app.state.settings.publish_enabled = False

    assert response.status_code == 200, response.text
    assert response.json()["trigger"] == "republish"
    assert state_of(sessions, revision_id) == RevisionState.PENDING


# =========================================================================
# ACCEPTANCE: a mock device, a real broker, a real worker
# =========================================================================


def _provision(url: str, kek: str, out) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--out", str(out), "--host", "127.0.0.1"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": url,
            "EOE_SESSION_SECRET": f"worker-{uuid.uuid4().hex}",
            "EOE_KEK": kek,
        },
        timeout=180,
    )
    assert result.returncode == 0, f"devbroker failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def live_stack(tmp_path_factory):
    """A provisioned broker plus its own database, for the acceptance tests."""
    out = tmp_path_factory.mktemp("worker-certs")
    port = free_port()
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        _provision(url, kek, out)
        with factory() as db:
            db.execute(update(DeploymentService).values(port=port))
            db.commit()
        with ephemeral_broker(out, host_port=port) as broker:
            yield broker, factory, SecretStore(factory, kek), load_manifest(out)


def device_credentials(manifest, aggregator_uuid: str = AGG) -> tuple[str, str]:
    username = device_username(aggregator_uuid)
    return username, next(a["password"] for a in manifest["accounts"] if a["username"] == username)


async def device_publishes(broker, manifest, topic: str, payload: bytes) -> None:
    """A mock Aggregator speaking with its OWN credential, so the spec 7.1 ACL
    is on trial in every one of these steps."""
    username, password = device_credentials(manifest)
    published = await asyncio.to_thread(
        broker.exec_client,
        "mosquitto_pub",
        "-h",
        "localhost",
        "-p",
        "8883",
        "--cafile",
        "/mosquitto/dev/ca.crt",
        "-u",
        username,
        "-P",
        password,
        "-q",
        "1",
        "-t",
        topic,
        "-m",
        payload.decode(),
    )
    assert published.returncode == 0, published.stderr


async def device_reads_desired(broker, manifest) -> DesiredConfig:
    username, password = device_credentials(manifest)
    subscribed = await asyncio.to_thread(
        broker.exec_client,
        "mosquitto_sub",
        "-h",
        "localhost",
        "-p",
        "8883",
        "--cafile",
        "/mosquitto/dev/ca.crt",
        "-u",
        username,
        "-P",
        password,
        "-t",
        desired_topic(RC, AGG),
        "-W",
        "10",
        "-C",
        "1",
    )
    assert subscribed.returncode == 0, subscribed.stderr
    return decode(DesiredConfig, subscribed.stdout.strip())


def report_payload(revision_id: uuid.UUID | None, config: dict) -> bytes:
    return encode(
        ReportedAggregatorState(
            reported_at=datetime.now(UTC),
            applied_revision_id=revision_id,
            config=config,
            checksum=config_checksum(config),
        )
    )


async def wait_for_state(factory, revision_id: uuid.UUID, wanted: str, timeout: float = 30.0):
    """Poll the database, because the worker's progress is only ever visible
    there — no test-side hook into the worker decides when a step is done."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        current = await asyncio.to_thread(state_of, factory, revision_id)
        if current == wanted:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"revision {revision_id} never reached {wanted}; it is "
        f"{await asyncio.to_thread(state_of, factory, revision_id)}"
    )


async def test_the_full_journey_publish_ack_drift_republish_and_timeout(live_stack):
    """ACCEPTANCE (phase doc E3.7). One mock device, every edge in order:

    publish -> pending, ack -> applied, quiet divergence -> drifted (found by
    the sweep, with no report that could have driven it), operator re-publish
    -> pending, silence -> failed(timeout).

    Everything is driven through the real worker: the manager it owns, the
    consumer it registered, and the sweeps it schedules. Nothing in this test
    calls a sweep or a transition directly.
    """
    broker, factory, store, manifest = live_stack
    with factory() as db:
        db.execute(
            update(Deployment).where(Deployment.slug == RC).values(pending_timeout_seconds=300)
        )
        db.commit()
    revision_id = make_revision(factory, state="draft", published_ago=None)

    worker = ReconciliationWorker(factory, store, timeout_interval=1.0, drift_interval=1.0)
    async with worker:
        await worker.manager.wait_connected(deployment_id_of(factory))

        # 1. publish -> pending, and the device is holding the bytes.
        await publish_revision(factory, worker.manager, revision_id, publish_enabled=True)
        assert state_of(factory, revision_id) == RevisionState.PENDING
        desired = await device_reads_desired(broker, manifest)
        assert desired.revision_id == revision_id
        assert desired.config == SNAPSHOT

        # 2. the device acks with exactly what it was given -> applied.
        await device_publishes(
            broker, manifest, reported_topic(RC, AGG), report_payload(revision_id, SNAPSHOT)
        )
        await wait_for_state(factory, revision_id, RevisionState.APPLIED)

        # 3. drift injection: the device quietly diverges and reports WITHOUT
        #    naming a revision, so nothing about that message moves a state.
        #    Only the sweep's re-comparison can find this.
        await device_publishes(
            broker, manifest, reported_topic(RC, AGG), report_payload(None, DIVERGED)
        )
        await wait_for_state(factory, revision_id, RevisionState.DRIFTED)
        (drift_row,) = audit_rows(factory, AUDIT_DRIFT, revision_id)
        assert drift_row.detail is not None
        assert drift_row.detail["found_by"] == "drift_sweep"

        # 4. the operator re-publishes over the drift -> pending again.
        outcome = await publish_revision(factory, worker.manager, revision_id, publish_enabled=True)
        assert outcome.trigger is not None and outcome.trigger.value == "republish"
        assert state_of(factory, revision_id) == RevisionState.PENDING

        # 5. this time the device says nothing, and the window closes.
        with factory() as db:
            db.execute(
                update(Deployment).where(Deployment.slug == RC).values(pending_timeout_seconds=1)
            )
            db.commit()
        await wait_for_state(factory, revision_id, RevisionState.FAILED)

    (timeout_row,) = audit_rows(factory, AUDIT_TIMEOUT, revision_id)
    assert timeout_row.detail is not None
    assert timeout_row.detail["trigger"] == "timeout"
    assert timeout_row.actor_user_id is None

    with factory() as db:
        db.execute(delete(ConfigRevision))
        db.execute(delete(DeviceState))
        db.execute(
            update(Deployment).where(Deployment.slug == RC).values(pending_timeout_seconds=300)
        )
        db.commit()


async def test_a_restarted_worker_resumes_the_windows_it_did_not_start(live_stack):
    """ACCEPTANCE (phase doc E3.7): the worker survives restart without losing
    state, because it never held any. The revision was published by one worker
    and timed out by a different one, and the retained desired message is
    still on the broker for a device that connects after both.
    """
    broker, factory, store, manifest = live_stack
    revision_id = make_revision(factory, state="draft", published_ago=None)

    first = ReconciliationWorker(factory, store, timeout_interval=3600, drift_interval=3600)
    async with first:
        await first.manager.wait_connected(deployment_id_of(factory))
        await publish_revision(factory, first.manager, revision_id, publish_enabled=True)
    assert state_of(factory, revision_id) == RevisionState.PENDING

    # Nothing is running now. The window is in Postgres and the desired config
    # is in a retained message; that is the whole handover.
    with factory() as db:
        db.execute(
            update(Deployment).where(Deployment.slug == RC).values(pending_timeout_seconds=1)
        )
        db.commit()
    await asyncio.sleep(1.1)

    second = ReconciliationWorker(factory, store, timeout_interval=0.5, drift_interval=3600)
    async with second:
        await wait_for_state(factory, revision_id, RevisionState.FAILED)
        desired = await device_reads_desired(broker, manifest)
        assert desired.revision_id == revision_id, (
            "the retained desired message outlived both workers (spec 6.4)"
        )

    with factory() as db:
        db.execute(delete(ConfigRevision))
        db.execute(
            update(Deployment).where(Deployment.slug == RC).values(pending_timeout_seconds=300)
        )
        db.commit()


async def test_the_worker_connects_to_every_deployments_broker(live_stack):
    """One connection per deployment (E3.2's model), created from the
    `deployment_service` rows and nothing else."""
    _broker, factory, store, _manifest = live_stack
    coordinates = load_broker_coordinates(factory, store)
    worker = ReconciliationWorker(factory, store, timeout_interval=3600, drift_interval=3600)
    async with worker:
        assert set(worker.manager.deployment_ids) == {c.deployment_id for c in coordinates}
        await worker.manager.wait_connected(deployment_id_of(factory))
        assert worker.manager.is_connected(deployment_id_of(factory))


async def test_a_manager_built_from_the_same_rows_publishes_and_the_worker_consumes(live_stack):
    """The API publishes on its own connection while the worker holds the
    subscriptions — the two-process shape D59 describes, proven to work
    against one broker rather than assumed."""
    _broker, factory, store, _manifest = live_stack
    revision_id = make_revision(factory, state="draft", published_ago=None)
    worker = ReconciliationWorker(factory, store, timeout_interval=3600, drift_interval=3600)
    api_side = MqttClientManager(lambda: load_broker_coordinates(factory, store))
    async with worker, api_side:
        deployment_id = deployment_id_of(factory)
        await worker.manager.wait_connected(deployment_id)
        await api_side.wait_connected(deployment_id)
        await publish_revision(factory, api_side, revision_id, publish_enabled=True)

    assert state_of(factory, revision_id) == RevisionState.PENDING
    with factory() as db:
        db.execute(delete(ConfigRevision))
        db.commit()


# =========================================================================
# The container healthcheck (D92)
# =========================================================================


async def test_the_heartbeat_is_written_while_the_sweeps_are_alive(sessions, tmp_path):
    """The compose `worker` has no port to probe, so the healthcheck reads the
    age of this file. It must exist and stay fresh while the worker works."""
    stamp = tmp_path / "beat"
    worker = ReconciliationWorker(
        sessions,
        SecretStore(sessions, make_kek()),
        manager=StubManager(),
        timeout_interval=3600,
        drift_interval=3600,
        heartbeat_path=stamp,
        heartbeat_interval=0.05,
    )
    async with worker:
        for _ in range(100):
            if stamp.exists():
                break
            await asyncio.sleep(0.05)
        assert stamp.exists(), "the container would never become healthy"
        first = stamp.read_text()
        await asyncio.sleep(0.2)
        assert stamp.read_text() >= first


async def test_a_dead_sweep_lets_the_heartbeat_go_stale(sessions, tmp_path):
    """THE reason this is a real check rather than a tick (D92).

    The failure a worker can actually suffer is a sweep task dying while the
    process stays up holding its broker connection — from outside
    indistinguishable from a healthy fleet. A heartbeat that only proved the
    process had not segfaulted would report that as healthy forever.
    """
    stamp = tmp_path / "beat"
    worker = ReconciliationWorker(
        sessions,
        SecretStore(sessions, make_kek()),
        manager=StubManager(),
        timeout_interval=3600,
        drift_interval=3600,
        heartbeat_path=stamp,
        heartbeat_interval=0.05,
    )
    async with worker:
        for _ in range(100):
            if stamp.exists():
                break
            await asyncio.sleep(0.05)
        assert stamp.exists()

        # Kill a sweep the way an unhandled error would.
        worker._sweeps[0].cancel()
        await asyncio.sleep(0.2)
        frozen = stamp.read_text()
        await asyncio.sleep(0.3)

        assert stamp.read_text() == frozen, (
            "the heartbeat kept ticking with a dead sweep, so the container "
            "would stay green while nothing was being reconciled"
        )


async def test_no_heartbeat_file_is_written_unless_one_is_asked_for(sessions, tmp_path):
    """`EOE_WORKER_IN_API` runs the worker inside a process that already has an
    HTTP healthcheck, and the suite runs it hundreds of times. Neither should
    litter the filesystem."""
    worker = ReconciliationWorker(
        sessions, SecretStore(sessions, make_kek()), manager=StubManager(), timeout_interval=3600
    )
    async with worker:
        await asyncio.sleep(0.1)
    assert list(tmp_path.iterdir()) == []
