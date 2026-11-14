"""Registration matching and record transitions (task E4.9; spec 8.5; phase-4
§2 structural choice; DECISIONS D168, D179).

A reconciler that **re-derives** each record's status from E3's evidence:
`device_state`, `config_revision` and `quarantined_report`. It reads those
tables and never writes them, and it never edits `consumer.py`. Because
status is re-derived rather than accumulated, running it twice, or after a
restart, gives the same rows (asserted).

## The rules, as the phase document states them

- **`flashed`** is operator-set only (`mark_flashed`, behind `PATCH
  /provisioning/records/{id}`). The platform cannot observe a card being
  written, so this is the one status a person asserts.
- **`registered`**: a `device_state` row exists for the record's device.
  First contact is exactly that row's existence; no first-seen column is
  needed.
- **`confirmed`**: that row's checksum equals the embedded revision's checksum,
  **and the status latches.** Confirmation is a statement about what
  happened at first boot, not a live comparison. A device that later takes
  new config legitimately must not fall out of `confirmed`.
- **`mismatch`**:
  - an identity quarantine (`mac_conflict`, `name_conflict`, `unknown_mac`)
    exists for the record's MAC, created at or after the record (an older one
    is history from before this card existed); or
  - the device reports a checksum that belongs to NO `config_revision` for that
    device at all.

  A checksum belonging to a LATER revision is not a mismatch: that is an
  operator who changed the config, and flagging it would fill the board with
  false alarms the first time anyone edits anything.

## Precedence

Latched `confirmed` > `mismatch` > `confirmed` (newly reached) > `registered`
> `flashed` (if the operator ever marked it) > `generated`. A `mismatch` whose
evidence later clears re-derives to whatever the evidence now says. Every
change is appended to `status_history` with a null actor (the system).
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import ConfigRevision, DeviceProvisioningRecord, DeviceState, QuarantinedReport

#: Spec 8.5 "registered device reports unexpected identity": the three
#: identity outcomes E1.5/E3.5 quarantine instead of mutating inventory.
IDENTITY_QUARANTINES = ("mac_conflict", "name_conflict", "unknown_mac")


class RecordTransitionError(ValueError):
    """An operator transition the rules do not allow."""


@dataclass
class RecordSweepReport:
    """What one pass changed. `changed` is what the worker's loop logs on."""

    examined: int = 0
    transitions: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.transitions)

    def __str__(self) -> str:
        moved = ", ".join(f"{k}={v}" for k, v in sorted(self.transitions.items())) or "none"
        return f"examined {self.examined} records; transitions {moved}"


def _ever(record: DeviceProvisioningRecord, status: str) -> bool:
    return any(entry.get("status") == status for entry in record.status_history)


def derive_status(db: Session, record: DeviceProvisioningRecord) -> str:
    """The status E3's evidence supports for this record now. Pure read."""
    if record.status == "confirmed":
        return "confirmed"  # latched (spec 8.5, phase-4 E4.9)

    if record.target_type == "listener":
        quarantined = db.scalar(
            select(QuarantinedReport.id)
            .where(
                QuarantinedReport.mac == record.target_id,
                QuarantinedReport.reason.in_(IDENTITY_QUARANTINES),
                QuarantinedReport.created_at >= record.created_at,
            )
            .limit(1)
        )
        if quarantined is not None:
            return "mismatch"

    state = db.scalar(
        select(DeviceState).where(
            DeviceState.entity_type == record.target_type,
            DeviceState.entity_id == record.target_id,
        )
    )
    if state is not None:
        if record.config_checksum is not None and state.checksum == record.config_checksum:
            return "confirmed"
        known = db.scalar(
            select(ConfigRevision.id)
            .where(
                ConfigRevision.target_type == record.target_type,
                ConfigRevision.target_id == record.target_id,
                ConfigRevision.checksum == state.checksum,
            )
            .limit(1)
        )
        if known is None:
            return "mismatch"
        return "registered"

    return "flashed" if _ever(record, "flashed") else "generated"


def _append(record: DeviceProvisioningRecord, status: str, actor: str | None) -> None:
    record.status = status
    record.status_history = [
        *record.status_history,
        {"status": status, "at": datetime.now(UTC).isoformat(), "actor": actor},
    ]


def reconcile_records(session_factory: sessionmaker[Session]) -> RecordSweepReport:
    """One pass over every record that can still move. Commits once.

    Idempotent by construction: a record whose derived status equals its
    stored one is untouched, history included, so a second pass over the same
    evidence writes nothing.
    """
    report = RecordSweepReport()
    with session_factory() as db:
        records = db.scalars(
            select(DeviceProvisioningRecord).where(DeviceProvisioningRecord.status != "confirmed")
        ).all()
        for record in records:
            report.examined += 1
            derived = derive_status(db, record)
            if derived != record.status:
                key = f"{record.status}->{derived}"
                report.transitions[key] = report.transitions.get(key, 0) + 1
                _append(record, derived, None)
        db.commit()
    return report


def mark_flashed(record: DeviceProvisioningRecord, actor_user_id: uuid.UUID) -> bool:
    """The operator's one transition: "this card is written". Returns whether
    anything changed (marking a flashed card again is a no-op, not an error).

    Only a `generated` card can be marked. A record the evidence has already
    moved past (registered, confirmed, mismatch) has nothing left for an
    operator to assert.
    """
    if record.status == "flashed":
        return False
    if record.status != "generated":
        raise RecordTransitionError(
            f"only a generated card can be marked flashed; this one is {record.status!r}"
        )
    _append(record, "flashed", str(actor_user_id))
    return True
