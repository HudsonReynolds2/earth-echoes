"""Gate 49: the reconciliation timeline (task E3.11; spec 6.3, 6.2, 13).

Spec 6.3 wants "every transition with a timestamp, the actor (user or
system), the before/after effective config diff, and any device-supplied
detail". The load-bearing word is EVERY, and
`test_no_transition_can_happen_without_a_timeline_row` is how that is held:
the row is written inside `revision_state.transition`, the single writer of
`config_revision.state`, so a timeline is complete by construction rather
than because each call site remembered.

`test_the_full_journey_renders_as_a_coherent_timeline` is the phase
document's acceptance — E3.7's own integration journey, read back as history.
"""

import uuid
from datetime import timedelta

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_auth import PASSWORD

from app.auth.passwords import hash_password
from app.config.canonical import config_checksum
from app.controlplane.revision_state import (
    TRANSITIONS,
    RevisionState,
    Trigger,
    supersede_open_revisions,
    transition,
)
from app.db import create_session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    ConfigRevision,
    Deployment,
    ReconciliationEvent,
    RoleAssignment,
    User,
    utcnow,
)
from app.seed import seed_demo_hierarchy
from app.settings import Settings

RC = "redwood-coast"
AGG = "demo-agg-rc-01"
MAC = "02:EE:0E:01:01:01"

OWNER = "tl-owner@example.com"
OTHER_OP = "tl-other-op@example.com"
VIEWER = "tl-viewer@example.com"

BASE = {"capture.mode": "continuous", "capture.sample_rate_hz": 48000}
CHANGED = {**BASE, "capture.sample_rate_hz": 22050}
SECRETY = {**BASE, "upload.s3_access_key": "secret://upload.s3_access_key"}

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
def sessions(database):
    yield database
    with database() as db:
        db.execute(delete(ReconciliationEvent))
        db.execute(delete(ConfigRevision))
        db.commit()


def deployment_id_of(factory) -> uuid.UUID:
    with factory() as db:
        return db.scalars(select(Deployment.id).where(Deployment.slug == RC)).one()


def platform_uuid_of(factory) -> str:
    with factory() as db:
        return str(db.scalars(select(Aggregator.id).where(Aggregator.aggregator_uuid == AGG)).one())


def make_revision(
    factory,
    *,
    state: str = "draft",
    snapshot: dict | None = None,
    target_type: str = "aggregator",
    target_id: str | None = None,
    age: timedelta = timedelta(0),
) -> uuid.UUID:
    body = BASE if snapshot is None else snapshot
    revision = ConfigRevision(
        target_type=target_type,
        target_id=target_id if target_id is not None else platform_uuid_of(factory),
        deployment_id=deployment_id_of(factory),
        snapshot=body,
        schema_version=1,
        checksum=config_checksum(body),
        state=state,
        created_at=utcnow() - age,
    )
    with factory() as db:
        db.add(revision)
        db.commit()
        return revision.id


def move(factory, revision_id: uuid.UUID, target, trigger, **kwargs):
    with factory() as db:
        revision = db.get(ConfigRevision, revision_id)
        record = transition(db, revision, target, trigger, **kwargs)
        db.commit()
        return record


def events_for(factory, target_id: str, target_type: str = "aggregator"):
    with factory() as db:
        return list(
            db.scalars(
                select(ReconciliationEvent)
                .where(
                    ReconciliationEvent.target_type == target_type,
                    ReconciliationEvent.target_id == target_id,
                )
                .order_by(ReconciliationEvent.at, ReconciliationEvent.id)
            ).all()
        )


# --- Completeness by construction -------------------------------------------


@pytest.mark.parametrize("row", sorted(TRANSITIONS, key=lambda r: (r.source, r.target, r.trigger)))
def test_no_transition_can_happen_without_a_timeline_row(sessions, row):
    """THE property (spec 6.3's "every transition"), over the spec 6.2 table
    row by row. The record is written inside `transition()`, which is the only
    writer of `config_revision.state`, so there is no path that moves a
    revision without leaving history."""
    revision_id = make_revision(sessions, state=row.source.value)
    move(sessions, revision_id, row.target, row.trigger)

    events = events_for(sessions, platform_uuid_of(sessions))
    assert len(events) == 1, f"{row.source} -> {row.target} left no timeline row"
    assert events[0].from_state == row.source.value
    assert events[0].to_state == row.target.value
    assert events[0].trigger == row.trigger.value
    assert events[0].revision_id == revision_id


def test_a_refused_transition_writes_no_history(sessions):
    """An illegal move did not happen, so it is not history. A row here would
    put a transition on an operator's screen that the machine rejected."""
    from app.controlplane.revision_state import IllegalTransition

    revision_id = make_revision(sessions, state="applied")
    with pytest.raises(IllegalTransition):
        move(sessions, revision_id, RevisionState.PENDING, Trigger.PUBLISH)

    assert events_for(sessions, platform_uuid_of(sessions)) == []


def test_the_actor_is_recorded_and_the_system_leaves_it_null(sessions):
    """Spec 6.3's "actor (user or system)". NULL means nobody did it, which is
    the entire content of a `failed(timeout)` entry (D70) — and must not be
    rendered as "unknown"."""
    someone = uuid.uuid4()
    published = make_revision(sessions)
    move(sessions, published, RevisionState.PENDING, Trigger.PUBLISH, actor_user_id=someone)
    move(sessions, published, RevisionState.FAILED, Trigger.TIMEOUT)

    events = events_for(sessions, platform_uuid_of(sessions))
    assert [e.actor_user_id for e in events] == [someone, None]


def test_supersession_writes_one_row_per_revision_it_closed(sessions):
    """`supersede_open_revisions` moves several revisions at once, and each is its own
    transition — a single summary row would hide which revisions were closed."""
    older = make_revision(sessions, state="pending", age=timedelta(minutes=5))
    other = make_revision(sessions, state="failed", age=timedelta(minutes=4))
    winner_id = make_revision(sessions)

    with sessions() as db:
        winner = db.get(ConfigRevision, winner_id)
        supersede_open_revisions(db, winner)
        db.commit()

    superseded = [
        e for e in events_for(sessions, platform_uuid_of(sessions)) if e.to_state == "superseded"
    ]
    assert {e.revision_id for e in superseded} == {older, other}
    assert all(e.trigger == "newer_revision" for e in superseded)


# --- The diff (spec 6.3's "before/after effective config diff") -------------


def test_entering_pending_records_what_changed(sessions):
    make_revision(sessions, state="applied", snapshot=BASE, age=timedelta(minutes=5))
    newer = make_revision(sessions, snapshot=CHANGED)

    move(sessions, newer, RevisionState.PENDING, Trigger.PUBLISH)

    entry = [e for e in events_for(sessions, platform_uuid_of(sessions)) if e.to_state == "pending"]
    assert entry[0].diff == {"capture.sample_rate_hz": {"before": 48000, "after": 22050}}


def test_the_first_revision_for_a_device_has_no_diff(sessions):
    """It is not a change from anything. Rendering the whole config as "added"
    would bury the one key an operator actually edited under the catalog."""
    first = make_revision(sessions)
    move(sessions, first, RevisionState.PENDING, Trigger.PUBLISH)

    events = events_for(sessions, platform_uuid_of(sessions))
    assert events[0].diff is None


def test_the_other_edges_carry_no_diff(sessions):
    """`pending -> applied` moved state, not the desired config. Repeating the
    diff on every hop would read as four separate config changes."""
    make_revision(sessions, state="applied", snapshot=BASE, age=timedelta(minutes=5))
    newer = make_revision(sessions, snapshot=CHANGED)
    move(sessions, newer, RevisionState.PENDING, Trigger.PUBLISH)
    move(sessions, newer, RevisionState.APPLIED, Trigger.REPORT_MATCH)

    applied = [
        e for e in events_for(sessions, platform_uuid_of(sessions)) if e.to_state == "applied"
    ]
    assert applied[0].diff is None


def test_a_secret_key_reaches_the_diff_as_a_marker_and_never_as_plaintext(sessions):
    """Rule R2. Both sides come from `config_revision.snapshot`, which holds
    spec 5.4 markers — so a diff CAN carry values safely, and this is the test
    that says so out loud rather than leaving it to be rediscovered."""
    make_revision(sessions, state="applied", snapshot=BASE, age=timedelta(minutes=5))
    newer = make_revision(sessions, snapshot=SECRETY)
    move(sessions, newer, RevisionState.PENDING, Trigger.PUBLISH)

    entry = [e for e in events_for(sessions, platform_uuid_of(sessions)) if e.to_state == "pending"]
    change = entry[0].diff["upload.s3_access_key"]
    assert change["after"] == "secret://upload.s3_access_key"
    assert change["before"] is None


def test_device_supplied_detail_stays_out_of_the_diff(sessions):
    """The two columns have two provenances. Whatever a device said lands in
    `detail` as key NAMES; `diff` is snapshots only."""
    revision_id = make_revision(sessions, state="pending")
    move(
        sessions,
        revision_id,
        RevisionState.FAILED,
        Trigger.REPORT_ERROR,
        detail={"differing_keys": ["capture.sample_rate_hz"]},
    )

    entry = events_for(sessions, platform_uuid_of(sessions))[0]
    assert entry.detail == {"differing_keys": ["capture.sample_rate_hz"]}
    assert entry.diff is None


# --- The API ----------------------------------------------------------------


@pytest.fixture(scope="module")
def api_app(database):
    with database() as db:
        url = db.get_bind().url.render_as_string(hide_password=False)
        rc_id = db.scalars(select(Deployment.id).where(Deployment.slug == RC)).one()
        other_id = db.scalars(select(Deployment.id).where(Deployment.slug != RC)).first()
    app = create_app(
        Settings(
            database_url=url,
            session_secret="gate49-test-secret",
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


def timeline(client, aggregator_id, **params):
    return client.get(f"{API_PREFIX}/aggregators/{aggregator_id}/timeline", params=params)


def test_the_full_journey_renders_as_a_coherent_timeline(api_app, sessions):
    """ACCEPTANCE (phase doc E3.11): E3.7's integration journey, read back.

    publish -> ack -> drift -> operator re-publish -> timeout, newest first,
    each entry naming what moved it and who.
    """
    operator = uuid.uuid4()
    make_revision(sessions, state="applied", snapshot=BASE, age=timedelta(minutes=10))
    revision_id = make_revision(sessions, snapshot=CHANGED)
    move(sessions, revision_id, RevisionState.PENDING, Trigger.PUBLISH, actor_user_id=operator)
    move(sessions, revision_id, RevisionState.APPLIED, Trigger.REPORT_MATCH)
    move(
        sessions,
        revision_id,
        RevisionState.DRIFTED,
        Trigger.REPORT_DIVERGED,
        detail={"differing_keys": ["capture.sample_rate_hz"], "found_by": "drift_sweep"},
    )
    move(sessions, revision_id, RevisionState.PENDING, Trigger.REPUBLISH, actor_user_id=operator)
    move(sessions, revision_id, RevisionState.FAILED, Trigger.TIMEOUT)

    response = timeline(login(api_app, OWNER), platform_uuid_of(sessions))
    assert response.status_code == 200, response.text
    items = response.json()["items"]

    assert [(i["from_state"], i["to_state"], i["trigger"]) for i in items] == [
        ("pending", "failed", "timeout"),
        ("drifted", "pending", "republish"),
        ("applied", "drifted", "report_diverged"),
        ("pending", "applied", "report_match"),
        ("draft", "pending", "publish"),
    ]
    # The two the operator did carry a name; the three the system did do not.
    assert [i["actor_user_id"] is not None for i in items] == [False, True, False, False, True]
    assert items[2]["detail"]["found_by"] == "drift_sweep"
    assert items[4]["diff"] == {"capture.sample_rate_hz": {"before": 48000, "after": 22050}}


def test_the_actor_email_is_resolved_for_display(api_app, sessions):
    with api_app.state.session_factory() as db:
        owner_id = db.scalars(select(User.id).where(User.email == OWNER)).one()
    revision_id = make_revision(sessions)
    move(sessions, revision_id, RevisionState.PENDING, Trigger.PUBLISH, actor_user_id=owner_id)

    items = timeline(login(api_app, OWNER), platform_uuid_of(sessions)).json()["items"]
    assert items[0]["actor_email"] == OWNER


def test_a_system_transition_has_no_actor_email(api_app, sessions):
    revision_id = make_revision(sessions, state="pending")
    move(sessions, revision_id, RevisionState.FAILED, Trigger.TIMEOUT)

    items = timeline(login(api_app, OWNER), platform_uuid_of(sessions)).json()["items"]
    assert items[0]["actor_email"] is None
    assert items[0]["actor_user_id"] is None


def test_the_timeline_can_be_filtered_by_revision_and_state(api_app, sessions):
    first = make_revision(sessions)
    move(sessions, first, RevisionState.PENDING, Trigger.PUBLISH)
    move(sessions, first, RevisionState.FAILED, Trigger.TIMEOUT)
    second = make_revision(sessions)
    move(sessions, second, RevisionState.PENDING, Trigger.PUBLISH)

    client = login(api_app, OWNER)
    agg = platform_uuid_of(sessions)
    assert timeline(client, agg, revision_id=str(first)).json()["total"] == 2
    assert timeline(client, agg, to_state="failed").json()["total"] == 1


def test_a_listener_has_its_own_timeline(api_app, sessions):
    """Keyed by MAC, the way `config_revision.target_id` is for a Listener
    (D75) — so a device's revisions and its history join without translating."""
    revision_id = make_revision(sessions, target_type="listener", target_id=MAC)
    move(sessions, revision_id, RevisionState.PENDING, Trigger.PUBLISH)

    client = login(api_app, OWNER)
    response = client.get(f"{API_PREFIX}/listeners/{MAC}/timeline")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    # And it is not on the Aggregator's.
    assert timeline(client, platform_uuid_of(sessions)).json()["total"] == 0


def test_a_viewer_may_read_a_timeline(api_app, sessions):
    """It says what happened to the device, not what may be done to it."""
    revision_id = make_revision(sessions)
    move(sessions, revision_id, RevisionState.PENDING, Trigger.PUBLISH)

    response = timeline(login(api_app, VIEWER), platform_uuid_of(sessions))
    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_an_operator_in_another_deployment_gets_404(api_app, sessions):
    """D35 again: a history is as much of an existence oracle as the device."""
    response = timeline(login(api_app, OTHER_OP), platform_uuid_of(sessions))
    assert response.status_code == 404


def test_an_unknown_device_is_404(api_app, sessions):
    assert timeline(login(api_app, OWNER), uuid.uuid4()).status_code == 404


def test_a_device_with_no_history_reads_as_empty_not_missing(api_app, sessions):
    """A freshly created Aggregator has never transitioned. Empty and 404 are
    different answers and the UI shows different things for them."""
    response = timeline(login(api_app, OWNER), platform_uuid_of(sessions))
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_history_outlives_the_revision_it_describes(api_app, sessions):
    """Un-FK'd on the D33 precedent. The timeline exists to answer questions
    about things that are gone; a cascade would erase exactly the history
    somebody is looking for."""
    revision_id = make_revision(sessions)
    move(sessions, revision_id, RevisionState.PENDING, Trigger.PUBLISH)
    with sessions() as db:
        db.execute(delete(ConfigRevision).where(ConfigRevision.id == revision_id))
        db.commit()

    response = timeline(login(api_app, OWNER), platform_uuid_of(sessions))
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["revision_id"] == str(revision_id)
