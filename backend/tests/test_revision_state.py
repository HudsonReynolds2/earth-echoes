"""Gate 42: E3.6 the config-revision state machine — TEST-CRITICAL (spec 14.5).

THIS SUITE IS THE DOCUMENTATION OF THE SPEC 6.2 LIFECYCLE and one of the four
suites no later session may weaken (rule R0). Its centre is `SPEC_6_2_TABLE`
below: a literal transcription of the spec's transition table, trigger text
included, so the document and the code can be held side by side and diffed by
eye. `test_the_transition_table_matches_spec_6_2_line_for_line` is the phase
document's acceptance criterion, stated as one assertion.

Everything legal is proven legal from that transcription; everything else is
proven illegal by ENUMERATION rather than by example — all 288 (source,
target, trigger) triples are generated and the 276 outside the legal set must
raise. A later session that adds a transition has to add it to the
transcription first, which means reading spec 6.2 again.

Two halves. The first is pure: no database, no fixtures, the table and the
guard. The second drives real `config_revision` rows, because the superseded
sweep and the row lock are claims about concurrency that cannot be proven
against an in-memory object.
"""

import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from test_auth import pg_url  # noqa: F401  (module fixture reuse)

from app.config.canonical import config_checksum
from app.controlplane.revision_state import (
    OPEN,
    TERMINAL,
    TRANSITIONS,
    IllegalTransition,
    RevisionState,
    Transition,
    Trigger,
    UnknownRevisionState,
    check,
    is_legal,
    legal_targets,
    legal_triggers,
    load_for_transition,
    open_revisions_for_target,
    parse_state,
    supersede_open_revisions,
    transition,
)
from app.db import create_session_factory
from app.models import Aggregator, ConfigRevision, Deployment, Organization, Pod

# ===========================================================================
# Spec 6.2, transcribed. Change this only by reading the specification.
# ===========================================================================

#: The transition table of spec section 6.2, verbatim: every row, in document
#: order, with its Trigger cell as written. The `Trigger` member each row maps
#: to is the third column.
SPEC_6_2_TABLE: tuple[tuple[str, str, str, str], ...] = (
    # From        To            Trigger (spec text)                              Trigger member
    ("draft", "pending", "operator publishes", "publish"),
    ("pending", "applied", "device reports matching state", "report_match"),
    ("pending", "failed", "device reports apply error", "report_error"),
    ("pending", "failed", "timeout elapses", "timeout"),
    ("pending", "superseded", "a newer revision is published before ack", "newer_revision"),
    ("applied", "drifted", "device reports state diverging from desired", "report_diverged"),
    ("applied", "superseded", "operator publishes a newer revision", "newer_revision"),
    ("drifted", "pending", "operator (or auto-reconcile policy) re-publishes", "republish"),
    ("failed", "pending", "operator retries", "retry"),
)

#: Spec 6.2's DIAGRAM draws one edge its table omits:
#:
#:     (any non-terminal) ──new revision──► superseded
#:
#: which reaches `draft`, `drifted` and `failed` as well as the two states the
#: table names. D69 takes the diagram; these are the rows that adds.
SPEC_6_2_DIAGRAM_EXTRA: tuple[tuple[str, str, str], ...] = (
    ("draft", "superseded", "newer_revision"),
    ("drifted", "superseded", "newer_revision"),
    ("failed", "superseded", "newer_revision"),
)

#: The spec 6.2 state table's six states, transcribed with their meanings so a
#: renamed member is caught here rather than at a device.
SPEC_6_2_STATES: tuple[tuple[str, str], ...] = (
    ("draft", "Operator is editing. Not published to any device."),
    ("pending", "Published to the device's desired topic, not yet acknowledged."),
    ("applied", "Device reported state matching this revision."),
    ("drifted", "Device previously applied, but reported state now diverges from desired."),
    ("failed", "Device reported an error applying, or the pending window timed out."),
    ("superseded", "A newer revision replaced this one."),
)

ALL_STATES = tuple(RevisionState)
ALL_TRIGGERS = tuple(Trigger)


def _expected_legal() -> set[tuple[RevisionState, RevisionState, Trigger]]:
    """The legal set, rebuilt from the transcriptions above and nothing else."""
    rows = {
        (RevisionState(source), RevisionState(target), Trigger(trigger))
        for source, target, _, trigger in SPEC_6_2_TABLE
    }
    rows |= {
        (RevisionState(source), RevisionState(target), Trigger(trigger))
        for source, target, trigger in SPEC_6_2_DIAGRAM_EXTRA
    }
    return rows


# ===========================================================================
# Part 1: the table and the guard. Pure — no database.
# ===========================================================================


def test_the_transition_table_matches_spec_6_2_line_for_line():
    """E3.6's acceptance criterion. The module's table and the specification's
    table are the same set of triples, neither adding nor dropping a row."""
    coded = {(row.source, row.target, row.trigger) for row in TRANSITIONS}
    assert coded == _expected_legal()


def test_every_spec_table_row_carries_its_trigger_text_unchanged():
    """The `spec_trigger` field is the audit trail for the transcription: if a
    row's meaning is edited in the module, the spec text stops matching."""
    coded = {(row.source, row.target, row.trigger): row.spec_trigger for row in TRANSITIONS}
    for source, target, spec_text, trigger in SPEC_6_2_TABLE:
        key = (RevisionState(source), RevisionState(target), Trigger(trigger))
        assert coded[key] == spec_text


def test_the_state_vocabulary_is_exactly_spec_6_2s_six():
    assert [state.value for state in ALL_STATES] == [name for name, _ in SPEC_6_2_STATES]


def test_superseded_is_the_only_terminal_state():
    """The diagram's "any non-terminal" is only well defined if exactly one
    state is terminal. Every other state must have somewhere to go."""
    assert set(TERMINAL) == {RevisionState.SUPERSEDED}
    assert not legal_targets(RevisionState.SUPERSEDED)
    for state in OPEN:
        assert legal_targets(state), f"{state} is not terminal but has no exit"


def test_open_and_terminal_partition_the_vocabulary():
    assert set(OPEN | TERMINAL) == set(ALL_STATES)
    assert not OPEN & TERMINAL


@pytest.mark.parametrize(("source", "target", "trigger"), sorted(_expected_legal()))
def test_every_legal_transition_is_accepted(source, target, trigger):
    assert is_legal(source, target, trigger)
    check(source, target, trigger)  # does not raise


def test_every_illegal_transition_is_rejected():
    """All 288 (source, target, trigger) triples are generated; the 276 that
    spec 6.2 does not name must raise. Enumeration rather than examples: an
    illegal move accepted by accident is exactly the defect this suite exists
    to prevent, and a hand-picked list of illegal cases cannot prove absence.

    One test rather than 276 parametrized ones, and it reports every offender
    at once — a change that widens the table wrongly usually widens it by more
    than a single triple, and the whole set is what identifies the mistake.
    """
    everything = set(itertools.product(ALL_STATES, ALL_STATES, ALL_TRIGGERS))
    legal = _expected_legal()
    assert len(everything) == 288
    assert len(legal) == 12

    wrongly_accepted = []
    for source, target, trigger in sorted(everything - legal):
        if is_legal(source, target, trigger):
            wrongly_accepted.append(f"{source} -> {target} ({trigger})")
            continue
        try:
            check(source, target, trigger)
        except IllegalTransition:
            continue
        wrongly_accepted.append(f"{source} -> {target} ({trigger}): check() did not raise")
    assert not wrongly_accepted, "spec 6.2 does not permit: " + "; ".join(wrongly_accepted)


def test_no_transition_leaves_a_terminal_state_under_any_trigger():
    for target, trigger in itertools.product(ALL_STATES, ALL_TRIGGERS):
        for terminal in TERMINAL:
            assert not is_legal(terminal, target, trigger)


def test_every_state_is_reachable_from_draft():
    """A state nothing can reach is dead vocabulary. Breadth-first from
    `draft`, the state E2 writes, must cover all six."""
    seen = {RevisionState.DRAFT}
    frontier = [RevisionState.DRAFT]
    while frontier:
        for reachable in legal_targets(frontier.pop()):
            if reachable not in seen:
                seen.add(reachable)
                frontier.append(reachable)
    assert seen == set(ALL_STATES)


def test_a_pair_can_be_legal_under_one_trigger_and_illegal_under_another():
    """Why the unit here is a triple and not a pair. `pending -> failed` is
    legal twice over and illegal under every other cause; validating the pair
    alone would accept a timeout reported as an operator retry."""
    assert legal_triggers(RevisionState.PENDING, RevisionState.FAILED) == frozenset(
        {Trigger.REPORT_ERROR, Trigger.TIMEOUT}
    )
    assert not is_legal(RevisionState.PENDING, RevisionState.FAILED, Trigger.RETRY)


def test_the_timeout_trigger_belongs_to_exactly_one_transition():
    """`timeout` means silence and nothing else (D70). A device that answered
    with the wrong config fails as `report_error` on its first report, so
    `failed(timeout)` never has to cover two different stories."""
    timeouts = {(row.source, row.target) for row in TRANSITIONS if row.trigger is Trigger.TIMEOUT}
    assert timeouts == {(RevisionState.PENDING, RevisionState.FAILED)}


# --- the messages, which are the whole point of a guard ---------------------


def test_a_no_op_transition_names_itself_as_one():
    with pytest.raises(IllegalTransition, match="to itself"):
        check(RevisionState.PENDING, RevisionState.PENDING, Trigger.PUBLISH)


def test_a_terminal_source_says_it_is_terminal():
    with pytest.raises(IllegalTransition, match="terminal"):
        check(RevisionState.SUPERSEDED, RevisionState.PENDING, Trigger.RETRY)


def test_a_legal_pair_under_the_wrong_trigger_names_the_permitted_triggers():
    """The most confusable failure gets the most specific message: the caller
    had the right intent and reported the wrong cause."""
    with pytest.raises(IllegalTransition) as raised:
        check(RevisionState.PENDING, RevisionState.FAILED, Trigger.RETRY)
    message = str(raised.value)
    assert "not under retry" in message
    assert "report_error" in message and "timeout" in message


def test_an_impossible_pair_lists_where_the_source_can_actually_go():
    with pytest.raises(IllegalTransition) as raised:
        check(RevisionState.DRAFT, RevisionState.APPLIED, Trigger.REPORT_MATCH)
    message = str(raised.value)
    assert "not a spec 6.2 transition" in message
    assert "pending" in message and "superseded" in message


def test_parse_state_rejects_anything_outside_the_vocabulary():
    with pytest.raises(UnknownRevisionState, match="applied"):
        parse_state("apllied")


@pytest.mark.parametrize("state", ALL_STATES)
def test_parse_state_round_trips_every_stored_value(state):
    assert parse_state(state.value) is state


def test_transition_rows_are_hashable_and_frozen():
    """`TRANSITIONS` is a frozenset of frozen dataclasses: the table cannot be
    mutated at runtime by a caller that got hold of it."""
    row = next(iter(TRANSITIONS))
    with pytest.raises(AttributeError):
        row.target = RevisionState.FAILED  # type: ignore[misc]
    assert isinstance(TRANSITIONS, frozenset)
    assert len(TRANSITIONS) == len(_expected_legal())
    assert all(isinstance(row, Transition) for row in TRANSITIONS)


# ===========================================================================
# Part 2: real rows. The sweep and the lock are concurrency claims.
# ===========================================================================


@pytest.fixture(scope="module")
def factory(pg_url):  # noqa: F811
    _, session_factory = create_session_factory(pg_url)
    return session_factory


@pytest.fixture(scope="module")
def world(factory):
    """One deployment, one aggregator, one listener-free pod — enough to carry
    revisions, which are un-FK'd to their targets anyway (D55)."""
    with factory() as db:
        org = Organization(name="rev-state-org")
        db.add(org)
        db.flush()
        dep = Deployment(organization_id=org.id, name="rev-state-dep", slug="rev-state-dep")
        db.add(dep)
        db.flush()
        pod = Pod(deployment_id=dep.id, name="rev-state-pod")
        db.add(pod)
        db.flush()
        agg = Aggregator(pod_id=pod.id, aggregator_uuid="rev-state-agg")
        db.add(agg)
        db.commit()
        return {"deployment_id": dep.id, "aggregator_id": agg.id}


def _revision(
    db,
    world,
    state: RevisionState,
    *,
    target_id: str | None = None,
    created_at: datetime | None = None,
) -> ConfigRevision:
    """One revision row. `created_at` is settable because every row written in
    one transaction otherwise shares `now()` — Postgres reports the
    TRANSACTION's timestamp — and a test about ordering needs the order to be
    real rather than the accident of two random UUIDs breaking a tie."""
    snapshot = {"logging.verbosity": "info", "marker": uuid.uuid4().hex}
    row = ConfigRevision(
        target_type="aggregator",
        target_id=target_id or str(world["aggregator_id"]),
        deployment_id=world["deployment_id"],
        snapshot=snapshot,
        checksum=config_checksum(snapshot),
        state=state.value,
    )
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.flush()
    return row


@pytest.mark.integration
def test_transition_writes_the_state_and_reports_what_it_did(factory, world):
    with factory() as db:
        revision = _revision(db, world, RevisionState.DRAFT, target_id=f"agg-{uuid.uuid4().hex}")
        actor = uuid.uuid4()
        record = transition(
            db,
            revision,
            RevisionState.PENDING,
            Trigger.PUBLISH,
            actor_user_id=actor,
            detail={"topic": "eoe/dep/agg/a/desired"},
        )
        assert revision.state == "pending"
        assert record.source is RevisionState.DRAFT
        assert record.target_state is RevisionState.PENDING
        assert record.trigger is Trigger.PUBLISH
        assert record.revision_id == revision.id
        assert record.actor_user_id == actor
        assert record.detail == {"topic": "eoe/dep/agg/a/desired"}
        db.rollback()


@pytest.mark.integration
def test_transition_never_commits_on_its_own(factory, world):
    """The `record_audit` convention: a transition and whatever the caller is
    doing alongside it seal or roll back together. A self-committing machine
    would leave a revision `pending` after the publish it describes failed."""
    target = f"agg-{uuid.uuid4().hex}"
    with factory() as db:
        revision = _revision(db, world, RevisionState.DRAFT, target_id=target)
        revision_id = revision.id
        transition(db, revision, RevisionState.PENDING, Trigger.PUBLISH)
        db.rollback()
    with factory() as db:
        assert db.get(ConfigRevision, revision_id) is None


@pytest.mark.integration
def test_an_illegal_transition_leaves_the_row_untouched(factory, world):
    with factory() as db:
        revision = _revision(db, world, RevisionState.APPLIED, target_id=f"agg-{uuid.uuid4().hex}")
        with pytest.raises(IllegalTransition):
            transition(db, revision, RevisionState.PENDING, Trigger.PUBLISH)
        assert revision.state == "applied"
        db.rollback()


@pytest.mark.integration
def test_a_row_holding_an_unknown_state_refuses_to_move(factory, world):
    """Loud rather than lenient: treating an unrecognized state as `draft`
    would republish live config to a device."""
    with factory() as db:
        revision = _revision(db, world, RevisionState.DRAFT, target_id=f"agg-{uuid.uuid4().hex}")
        revision.state = "half-applied"
        with pytest.raises(UnknownRevisionState):
            transition(db, revision, RevisionState.PENDING, Trigger.PUBLISH)
        db.rollback()


# --- the superseded path: a newer revision preempts a pending one -----------


@pytest.mark.integration
def test_a_newer_revision_supersedes_the_pending_one_it_preempts(factory, world):
    """The phase document's named case. Revision A is pending on the device;
    B is published; A is superseded and B is untouched."""
    target = f"agg-{uuid.uuid4().hex}"
    with factory() as db:
        older = _revision(db, world, RevisionState.PENDING, target_id=target)
        newer = _revision(db, world, RevisionState.DRAFT, target_id=target)
        records = supersede_open_revisions(db, newer)
        assert [record.revision_id for record in records] == [older.id]
        assert older.state == "superseded"
        assert newer.state == "draft"
        assert records[0].trigger is Trigger.NEWER_REVISION
        assert records[0].detail == {"superseded_by": str(newer.id), "superseded_from": "pending"}
        db.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("state", sorted(OPEN))
def test_every_open_state_is_superseded_by_a_newer_revision(factory, world, state):
    """The diagram's edge, exercised from all five non-terminal states — the
    three the table omits included (D69). A `failed` revision left un-closable
    is the concrete cost of taking the table alone."""
    target = f"agg-{uuid.uuid4().hex}"
    with factory() as db:
        older = _revision(db, world, state, target_id=target)
        newer = _revision(db, world, RevisionState.DRAFT, target_id=target)
        supersede_open_revisions(db, newer)
        assert older.state == "superseded"
        db.rollback()


@pytest.mark.integration
def test_superseding_never_touches_an_already_terminal_revision(factory, world):
    """Terminal means terminal: a second publish must not re-supersede rows
    the first one closed, which would double every timeline entry."""
    target = f"agg-{uuid.uuid4().hex}"
    with factory() as db:
        done = _revision(db, world, RevisionState.SUPERSEDED, target_id=target)
        newer = _revision(db, world, RevisionState.DRAFT, target_id=target)
        assert supersede_open_revisions(db, newer) == []
        assert done.state == "superseded"
        db.rollback()


@pytest.mark.integration
def test_superseding_is_scoped_to_one_device(factory, world):
    """Revisions are per-device (D55). Publishing to one aggregator must not
    close another's — the failure mode would be silent and fleet-wide."""
    mine = f"agg-{uuid.uuid4().hex}"
    theirs = f"agg-{uuid.uuid4().hex}"
    with factory() as db:
        neighbour = _revision(db, world, RevisionState.PENDING, target_id=theirs)
        newer = _revision(db, world, RevisionState.DRAFT, target_id=mine)
        assert supersede_open_revisions(db, newer) == []
        assert neighbour.state == "pending"
        db.rollback()


@pytest.mark.integration
def test_superseding_is_idempotent(factory, world):
    target = f"agg-{uuid.uuid4().hex}"
    with factory() as db:
        older = _revision(db, world, RevisionState.PENDING, target_id=target)
        newer = _revision(db, world, RevisionState.DRAFT, target_id=target)
        assert len(supersede_open_revisions(db, newer)) == 1
        assert supersede_open_revisions(db, newer) == []
        assert older.state == "superseded"
        db.rollback()


@pytest.mark.integration
def test_open_revisions_exclude_terminal_rows_and_come_back_oldest_first(factory, world):
    target = f"agg-{uuid.uuid4().hex}"
    base = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    with factory() as db:
        first = _revision(db, world, RevisionState.FAILED, target_id=target, created_at=base)
        second = _revision(
            db, world, RevisionState.PENDING, target_id=target, created_at=base + timedelta(hours=1)
        )
        _revision(
            db,
            world,
            RevisionState.SUPERSEDED,
            target_id=target,
            created_at=base + timedelta(hours=2),
        )
        db.flush()
        found = open_revisions_for_target(db, "aggregator", target, lock=False)
        assert [row.id for row in found] == [first.id, second.id]
        without_first = open_revisions_for_target(
            db, "aggregator", target, exclude=first.id, lock=False
        )
        assert without_first == [second]
        db.rollback()


# --- the row lock: an ack and a timeout can land together -------------------


@pytest.mark.integration
def test_load_for_transition_takes_a_row_lock(factory, world):
    """The reported consumer and the timeout sweep race for the same pending
    revision. Proven by holding the lock in one transaction and watching a
    second `SELECT ... FOR UPDATE NOWAIT` fail rather than read through it."""
    target = f"agg-{uuid.uuid4().hex}"
    with factory() as setup:
        revision = _revision(setup, world, RevisionState.PENDING, target_id=target)
        revision_id = revision.id
        setup.commit()

    with factory() as holder:
        held = load_for_transition(holder, revision_id)
        assert held is not None and held.state == "pending"
        with factory() as contender:
            with pytest.raises(OperationalError):
                contender.execute(
                    text("SELECT id FROM config_revision WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": str(revision_id)},
                )
            contender.rollback()
        holder.rollback()


@pytest.mark.integration
def test_the_second_writer_of_a_racing_pair_is_refused_by_the_machine(factory, world):
    """The lock serializes; the guard then decides. An ack that wins the race
    moves pending -> applied, and the timeout that arrives behind it re-reads
    `applied` and is refused — rather than overwriting a true `applied` with a
    `failed` that never happened."""
    target = f"agg-{uuid.uuid4().hex}"
    with factory() as db:
        revision = _revision(db, world, RevisionState.PENDING, target_id=target)
        transition(db, revision, RevisionState.APPLIED, Trigger.REPORT_MATCH)
        with pytest.raises(IllegalTransition):
            transition(db, revision, RevisionState.FAILED, Trigger.TIMEOUT)
        assert revision.state == "applied"
        db.rollback()


@pytest.mark.integration
def test_the_full_spec_6_2_journey_runs_end_to_end(factory, world):
    """Every state a device's config actually passes through, in order, on one
    row: publish, ack, drift, re-publish, timeout, retry, then superseded by a
    newer revision. If this reads like the spec 6.2 diagram, it is meant to."""
    target = f"agg-{uuid.uuid4().hex}"
    with factory() as db:
        revision = _revision(db, world, RevisionState.DRAFT, target_id=target)
        journey = [
            (RevisionState.PENDING, Trigger.PUBLISH),
            (RevisionState.APPLIED, Trigger.REPORT_MATCH),
            (RevisionState.DRIFTED, Trigger.REPORT_DIVERGED),
            (RevisionState.PENDING, Trigger.REPUBLISH),
            (RevisionState.FAILED, Trigger.TIMEOUT),
            (RevisionState.PENDING, Trigger.RETRY),
        ]
        for state, trigger in journey:
            transition(db, revision, state, trigger)
            assert revision.state == state.value
        newer = _revision(db, world, RevisionState.DRAFT, target_id=target)
        supersede_open_revisions(db, newer)
        assert revision.state == "superseded"
        db.rollback()


@pytest.mark.integration
def test_the_state_column_accepts_every_state_the_machine_writes(factory, world):
    """`config_revision.state` is a plain string column with no CHECK (D55).
    This walks each value through a real INSERT so a later migration that adds
    the constraint cannot silently exclude one of the six."""
    with factory() as db:
        for state in ALL_STATES:
            row = _revision(db, world, state, target_id=f"agg-{uuid.uuid4().hex}")
            db.flush()
            assert db.scalar(select(ConfigRevision.state).where(ConfigRevision.id == row.id)) == (
                state.value
            )
        db.rollback()
