"""The config-revision state machine (task E3.6; spec 6.2) — TEST-CRITICAL.

**One of the four suites no later session may weaken (spec 14.5, rule R0).**
`backend/tests/test_revision_state.py` is the documentation of these
semantics; this module is the only place in the codebase that decides whether
a revision may move from one state to another, and `transition()` is the ONLY
writer of `config_revision.state`. E3.4 (publish), E3.5 (reported consumer)
and E3.7 (the worker) all come through here rather than assigning the column,
which is what keeps the spec 6.2 lifecycle true of every row instead of true
of whichever call site was written most recently.

The vocabulary is spec 6.2's, verbatim: `draft`, `pending`, `applied`,
`drifted`, `failed`, `superseded`. E2 writes `draft` and nothing else (D55);
every other value on that column was written here.

**A transition is a TRIPLE, not a pair.** The spec 6.2 table has a Trigger
column, and legality depends on it: `pending -> failed` is legal because a
device reported an apply error or the window elapsed, and illegal as an
"operator retries", which is `failed -> pending` read backwards. Validating
(source, target) alone would accept that, so the unit here is
`(source, target, trigger)` and `TRANSITIONS` transcribes the table row for
row — trigger text included, so a reader can hold the two side by side.

**Superseded, and the one place spec 6.2 contradicts itself (D69).** Its table
lists only `pending -> superseded` and `applied -> superseded`; its diagram,
four lines below, reads `(any non-terminal) --new revision--> superseded`. The
diagram wins here, so `draft`, `drifted` and `failed` may be superseded too.
Without it a revision that failed can never be closed out: the operator fixes
the config, publishes a new revision, and the old row sits at `failed` forever
next to an `applied` one, with nothing in the timeline saying which is live.
`superseded` is the only terminal state — every other state has a way out,
which is why "non-terminal" in the diagram means "anything but superseded".
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ConfigRevision, utcnow


class RevisionState(StrEnum):
    """Spec 6.2's six states. A `StrEnum` so a member compares equal to the
    string in `config_revision.state` without either side converting."""

    DRAFT = "draft"
    PENDING = "pending"
    APPLIED = "applied"
    DRIFTED = "drifted"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class Trigger(StrEnum):
    """Spec 6.2's Trigger column, as machine-readable causes.

    These are what the timeline renders (E3.11) and what makes a state change
    legible after the fact: `failed` alone cannot tell an operator whether the
    device rejected the config or simply never answered, and those call for
    opposite responses.
    """

    #: draft -> pending. "operator publishes"
    PUBLISH = "publish"
    #: pending -> applied. "device reports matching state"
    REPORT_MATCH = "report_match"
    #: pending -> failed. "device reports apply error"
    REPORT_ERROR = "report_error"
    #: pending -> failed. "timeout elapses" — and ONLY that. A device that
    #: answered with the wrong config fails as REPORT_ERROR on its first
    #: report (D70), so this trigger means silence and nothing else.
    TIMEOUT = "timeout"
    #: applied -> drifted. "device reports state diverging from desired"
    REPORT_DIVERGED = "report_diverged"
    #: drifted -> pending. "operator (or auto-reconcile policy) re-publishes".
    #: The policy half is inert in this phase: `auto_reconcile` is stored,
    #: defaults off, and waits on the spec 17 item 3 decision.
    REPUBLISH = "republish"
    #: failed -> pending. "operator retries"
    RETRY = "retry"
    #: anything non-terminal -> superseded. "a newer revision replaced this
    #: one" — the spec 6.2 diagram's edge (D69).
    NEWER_REVISION = "newer_revision"


#: The only state with no way out. Everything else is reachable-from and
#: leads-somewhere, which is what the spec 6.2 diagram means by "non-terminal".
TERMINAL: frozenset[RevisionState] = frozenset({RevisionState.SUPERSEDED})

#: States a revision can still move out of — the set the diagram's superseded
#: edge is drawn from, and the set E3.7 scans when a newer revision lands.
OPEN: frozenset[RevisionState] = frozenset(RevisionState) - TERMINAL


@dataclass(frozen=True)
class Transition:
    """One row of the spec 6.2 table. `spec_trigger` carries that row's
    Trigger cell verbatim so the document and the code can be diffed by eye;
    it is documentation, never matched on."""

    source: RevisionState
    target: RevisionState
    trigger: Trigger
    spec_trigger: str


def _rows() -> Iterable[Transition]:
    state = RevisionState
    # Spec 6.2's transition table, top to bottom, unabridged.
    yield Transition(state.DRAFT, state.PENDING, Trigger.PUBLISH, "operator publishes")
    yield Transition(
        state.PENDING, state.APPLIED, Trigger.REPORT_MATCH, "device reports matching state"
    )
    yield Transition(
        state.PENDING, state.FAILED, Trigger.REPORT_ERROR, "device reports apply error"
    )
    yield Transition(state.PENDING, state.FAILED, Trigger.TIMEOUT, "timeout elapses")
    yield Transition(
        state.PENDING,
        state.SUPERSEDED,
        Trigger.NEWER_REVISION,
        "a newer revision is published before ack",
    )
    yield Transition(
        state.APPLIED,
        state.DRIFTED,
        Trigger.REPORT_DIVERGED,
        "device reports state diverging from desired",
    )
    yield Transition(
        state.APPLIED,
        state.SUPERSEDED,
        Trigger.NEWER_REVISION,
        "operator publishes a newer revision",
    )
    yield Transition(
        state.DRIFTED,
        state.PENDING,
        Trigger.REPUBLISH,
        "operator (or auto-reconcile policy) re-publishes",
    )
    yield Transition(state.FAILED, state.PENDING, Trigger.RETRY, "operator retries")
    # Spec 6.2's DIAGRAM, which draws the superseded edge from every
    # non-terminal state and not only the two the table names (D69).
    for source in sorted(OPEN - {state.PENDING, state.APPLIED}):
        yield Transition(
            source, state.SUPERSEDED, Trigger.NEWER_REVISION, "(any non-terminal) new revision"
        )


#: Every legal move, as (source, target, trigger) triples.
TRANSITIONS: frozenset[Transition] = frozenset(_rows())

_LEGAL: frozenset[tuple[RevisionState, RevisionState, Trigger]] = frozenset(
    (row.source, row.target, row.trigger) for row in TRANSITIONS
)


class IllegalTransition(Exception):
    """A move this machine refuses.

    A plain exception rather than the API's `AppError`: the worker and the
    reported consumer run outside the HTTP layer and must not have an error
    envelope imposed on them (the `ContractError` precedent). Callers at the
    API boundary translate it; a raise reaching a device-driven path is a bug
    in the caller, not a device's fault, and belongs in the log as one.
    """


class UnknownRevisionState(Exception):
    """A `config_revision.state` value outside the spec 6.2 vocabulary.

    Reachable only by a hand-edited row or a future migration that added a
    state without teaching this module about it. Loud on purpose: silently
    treating an unrecognized state as `draft` would republish live config.
    """


def parse_state(value: str) -> RevisionState:
    """The stored string as a state, or `UnknownRevisionState`."""
    try:
        return RevisionState(value)
    except ValueError as error:
        raise UnknownRevisionState(
            f"{value!r} is not a spec 6.2 revision state; expected one of "
            f"{', '.join(sorted(RevisionState))}"
        ) from error


def is_legal(source: RevisionState, target: RevisionState, trigger: Trigger) -> bool:
    return (source, target, trigger) in _LEGAL


def legal_targets(source: RevisionState) -> frozenset[RevisionState]:
    """Every state reachable from `source`, under any trigger."""
    return frozenset(row.target for row in TRANSITIONS if row.source == source)


def legal_triggers(source: RevisionState, target: RevisionState) -> frozenset[Trigger]:
    """Every trigger that legally moves `source` to `target` — empty when the
    pair itself is illegal."""
    return frozenset(
        row.trigger for row in TRANSITIONS if row.source == source and row.target == target
    )


def check(source: RevisionState, target: RevisionState, trigger: Trigger) -> None:
    """Raise `IllegalTransition` unless the triple is in the spec 6.2 table.

    The message distinguishes the three ways a move can be wrong, because they
    call for different fixes: a no-op is usually a missing idempotency check in
    the caller, a bad trigger is usually the right intent reported under the
    wrong cause, and a bad pair is a genuine lifecycle error.
    """
    if is_legal(source, target, trigger):
        return
    if source == target:
        raise IllegalTransition(
            f"a revision cannot transition from {source} to itself; "
            "callers that mean 'already there' check the state first"
        )
    if source in TERMINAL:
        raise IllegalTransition(
            f"{source} is terminal (spec 6.2): no trigger moves a revision out of it, "
            f"and {trigger} was offered to reach {target}"
        )
    permitted = legal_triggers(source, target)
    if permitted:
        raise IllegalTransition(
            f"{source} -> {target} is legal, but not under {trigger}; spec 6.2 allows "
            f"{', '.join(sorted(permitted))}"
        )
    reachable = legal_targets(source)
    raise IllegalTransition(
        f"{source} -> {target} is not a spec 6.2 transition; from {source} a revision "
        f"can only reach {', '.join(sorted(reachable)) or '(nothing)'}"
    )


@dataclass(frozen=True)
class TransitionRecord:
    """What happened, for the caller's audit row and for E3.11's timeline.

    `actor_user_id` is None for system-driven moves — a timeout, a device
    report — which is the same convention `record_audit` already uses for
    system-originated rows, so the two surfaces agree on what "no actor" looks
    like.
    """

    revision_id: uuid.UUID
    target_type: str
    target_id: str
    deployment_id: uuid.UUID
    source: RevisionState
    target_state: RevisionState
    trigger: Trigger
    at: datetime
    actor_user_id: uuid.UUID | None = None
    detail: dict[str, Any] | None = None


def transition(
    db: Session,
    revision: ConfigRevision,
    target: RevisionState,
    trigger: Trigger,
    *,
    actor_user_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> TransitionRecord:
    """Move one revision, or raise. **The only writer of `state`.**

    Stages the change on the caller's session and never commits — the
    `record_audit` convention, so a transition and whatever else the caller is
    doing seal or roll back together.

    E3.11 hangs the `reconciliation_event` row off this one function rather
    than off each call site: every state change in the system passes through
    here, so recording the timeline in one place is what makes the timeline
    complete by construction instead of by everyone remembering.

    **`published_at` is stamped here too, for the same reason** (E3.7). Spec
    6.2 reaches `pending` by three edges — a first publish, a retry after
    `failed`, a re-publish over `drifted` — and the spec 6.4 item 4 timeout
    window restarts on all three. Stamping it in the publisher would mean the
    window depended on which call site moved the revision; stamping it here
    means it is a property of entering the state.
    """
    source = parse_state(revision.state)
    check(source, target, trigger)
    revision.state = target.value
    at = utcnow()
    if target is RevisionState.PENDING:
        revision.published_at = at
    return TransitionRecord(
        revision_id=revision.id,
        target_type=revision.target_type,
        target_id=revision.target_id,
        deployment_id=revision.deployment_id,
        source=source,
        target_state=target,
        trigger=trigger,
        at=at,
        actor_user_id=actor_user_id,
        detail=detail,
    )


def load_for_transition(db: Session, revision_id: uuid.UUID) -> ConfigRevision | None:
    """Read one revision under a row lock, for callers about to transition it.

    The publisher, the reported consumer and the timeout sweep all run
    concurrently against the same rows — a device's ack can land in the same
    millisecond the window elapses. Without the lock both read `pending`, both
    pass `check`, and the later write wins silently; with it the second caller
    re-reads the state the first one wrote and its `check` refuses, which is
    the outcome that leaves a true row behind.
    """
    return db.scalars(
        select(ConfigRevision).where(ConfigRevision.id == revision_id).with_for_update()
    ).first()


def open_revisions_for_target(
    db: Session,
    target_type: str,
    target_id: str,
    *,
    exclude: uuid.UUID | None = None,
    lock: bool = True,
) -> list[ConfigRevision]:
    """Every revision for one device that is not yet terminal, oldest first.

    This is the set a newly published revision preempts, and the set an
    operator sees as "still live" on the device. Locked by default for the
    same reason `load_for_transition` is.
    """
    statement = (
        select(ConfigRevision)
        .where(
            ConfigRevision.target_type == target_type,
            ConfigRevision.target_id == target_id,
            ConfigRevision.state.in_([state.value for state in sorted(OPEN)]),
        )
        .order_by(ConfigRevision.created_at, ConfigRevision.id)
    )
    if exclude is not None:
        statement = statement.where(ConfigRevision.id != exclude)
    if lock:
        statement = statement.with_for_update()
    return list(db.scalars(statement).all())


def supersede_open_revisions(
    db: Session,
    winner: ConfigRevision,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> list[TransitionRecord]:
    """Supersede every OTHER open revision for the winner's device.

    Called when `winner` is published: from that moment the retained desired
    topic carries `winner` and nothing else, so no other revision for that
    device is the desired state any more — whatever state it was left in.
    That is the spec 6.2 diagram's edge, and superseding an older `draft` or
    `failed` row is the point of taking the diagram over the table (D69).

    Deliberately unconditional on timestamps. It supersedes anything open that
    is not the winner, which is only safe because E3.4 refuses to publish a
    revision that is not the newest for its device — otherwise this would
    quietly discard a newer draft the operator was still working on. The two
    rules are a pair; removing either one alone is a data-loss bug.
    """
    records = []
    for revision in open_revisions_for_target(
        db, winner.target_type, winner.target_id, exclude=winner.id
    ):
        # Read before the call: `transition` overwrites `state`, and relying on
        # argument-evaluation order to capture the old value would be a trap.
        was = revision.state
        records.append(
            transition(
                db,
                revision,
                RevisionState.SUPERSEDED,
                Trigger.NEWER_REVISION,
                actor_user_id=actor_user_id,
                detail={"superseded_by": str(winner.id), "superseded_from": was},
            )
        )
    return records
