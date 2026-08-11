"""Report-time identity handling (task E1.5; spec 4.3 items 2-3; D37).

Callable WITHOUT MQTT: E3.5 wires live broker messages into these functions
and must not reimplement their logic (phase-3 inherited interfaces). The API
is return-based - expected conditions come back as an IdentityResolution, not
exceptions - because E3's consumer loop handles every outcome; callers who
want to raise use the companion exceptions on `.outcome`.

Contract highlights (all suite-proven in test_identity_service.py):
- Matching is by MAC; conflicts NEVER touch inventory rows - the conflicting
  report is quarantined (spec 4.3 item 2, "rather than overwriting inventory").
- Unprovisioned detection is a MEMBERSHIP check against inventory, never
  equality to a sentinel value (spec 4.3 item 3).
- Quarantine rows append (every report is evidence); alerts dedupe on the
  open alert per (type, entity) via a partial unique index. `quarantine_report`
  is public so a channel can quarantine an outcome this module deliberately
  has no opinion on, using the same row shape (E3.5, D76).
- `duplicate_identity` / `provisioning_required` are ALERT TYPES, not wire
  error codes: the closed D8 vocabulary is not extended.
- Functions stage rows and never commit (the record_audit convention); the
  caller owns the transaction.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.inventory.naming import normalize_mac
from app.models import Aggregator, InventoryAlert, Listener, QuarantinedReport

ALERT_DUPLICATE_IDENTITY = "duplicate_identity"
ALERT_PROVISIONING_REQUIRED = "provisioning_required"


class IdentityOutcome(StrEnum):
    MATCHED = "matched"
    NAME_CONFLICT = "name_conflict"
    MAC_CONFLICT = "mac_conflict"
    PROVISIONING_REQUIRED = "provisioning_required"
    UNKNOWN_MAC = "unknown_mac"


@dataclass(frozen=True)
class ReportedIdentity:
    """What a device (or the sim, or a test) claims to be. `raw` carries the
    full source payload verbatim for the quarantine record."""

    mac: str
    aggregator_uuid: str
    name: str | None = None
    reported_at: datetime | None = None
    source: str = "test"  # "mqtt" | "sim" | "test"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IdentityResolution:
    outcome: IdentityOutcome
    listener: Listener | None = None
    quarantined: QuarantinedReport | None = None
    alert: InventoryAlert | None = None


class ProvisioningRequiredError(Exception):
    """Raised by require_known_aggregator for callers that want the raising
    style; handle_reported_identity itself never raises for this."""


class DuplicateIdentityError(Exception):
    """Companion for callers that prefer raising on conflict outcomes."""


def check_aggregator_membership(db: Session, aggregator_uuid: str) -> Aggregator | None:
    """Membership lookup against inventory - never equality to a sentinel
    (spec 4.3 item 3). None means the reporter is unprovisioned."""
    return db.scalars(
        select(Aggregator).where(Aggregator.aggregator_uuid == aggregator_uuid)
    ).first()


def require_known_aggregator(db: Session, aggregator_uuid: str) -> Aggregator:
    """The raising variant: opens (or reuses) a provisioning_required alert
    and raises. E3's metrics/objects ingest paths use this shape."""
    aggregator = check_aggregator_membership(db, aggregator_uuid)
    if aggregator is None:
        _open_alert(
            db,
            alert_type=ALERT_PROVISIONING_REQUIRED,
            entity_type="aggregator",
            entity_key=aggregator_uuid,
            deployment_id=None,
            detail={"aggregator_uuid": aggregator_uuid},
        )
        raise ProvisioningRequiredError(aggregator_uuid)
    return aggregator


def _open_alert(
    db: Session,
    *,
    alert_type: str,
    entity_type: str,
    entity_key: str,
    deployment_id: uuid.UUID | None,
    detail: dict[str, Any] | None,
) -> InventoryAlert:
    """Return the existing OPEN alert for (type, entity) or stage a new one.
    The partial unique index backstops this check against races."""
    existing = db.scalars(
        select(InventoryAlert).where(
            InventoryAlert.alert_type == alert_type,
            InventoryAlert.entity_type == entity_type,
            InventoryAlert.entity_key == entity_key,
            InventoryAlert.resolved_at.is_(None),
        )
    ).first()
    if existing is not None:
        return existing
    alert = InventoryAlert(
        alert_type=alert_type,
        entity_type=entity_type,
        entity_key=entity_key,
        deployment_id=deployment_id,
        detail=detail,
    )
    db.add(alert)
    db.flush()
    record_audit(
        db,
        action="inventory.alert",
        entity_type=entity_type,
        entity_id=entity_key,
        actor_user_id=None,  # system-originated
        scope=deployment_id,
        detail={"alert_type": alert_type},
    )
    return alert


def quarantine_report(db: Session, report: ReportedIdentity, reason: str) -> QuarantinedReport:
    """Stage one quarantine row plus its audit row. Public since E3.5.

    `handle_reported_identity` uses this for the two conflict outcomes it
    detects; it is exported so the channels that decide what a NON-conflict
    means can reuse the same row shape instead of assembling their own. E3.5's
    reported channel quarantines UNKNOWN_MAC that way (D76) - a Listener
    reporting before anyone entered it in inventory is not a conflict, so
    E1.5 correctly has no opinion, but it is worth an operator seeing.

    Stages only; the caller commits.
    """
    mac = normalize_mac(report.mac)
    row = QuarantinedReport(
        mac=mac,
        reported_name=report.name,
        aggregator_uuid=report.aggregator_uuid,
        reason=reason,
        report={"source": report.source, **report.raw},
    )
    db.add(row)
    db.flush()
    record_audit(
        db,
        action="inventory.quarantine",
        entity_type="listener",
        entity_id=mac,
        actor_user_id=None,
        detail={"reason": reason},
    )
    return row


def handle_reported_identity(db: Session, report: ReportedIdentity) -> IdentityResolution:
    """Spec 4.3 item 2, as a pure service. Match by MAC; on any disagreement
    quarantine the REPORT and alert - the inventory row is never modified.

    Outcomes:
    - PROVISIONING_REQUIRED: reporting aggregator_uuid matches no inventory
      row (membership check). Alert; no quarantine (there is no identity
      conflict, just an unknown reporter).
    - UNKNOWN_MAC: reporter is known, MAC is not in inventory. No side
      effects - E3 decides what an unregistered device means per channel.
    - MAC_CONFLICT: the MAC exists but under a different aggregator - two
      devices reporting one MAC, or a device on the wrong parent.
    - NAME_CONFLICT: same device, reported name disagrees with inventory.
    - MATCHED: everything agrees.

    Stages rows only; the caller commits.
    """
    mac = normalize_mac(report.mac)
    reporting = check_aggregator_membership(db, report.aggregator_uuid)
    if reporting is None:
        alert = _open_alert(
            db,
            alert_type=ALERT_PROVISIONING_REQUIRED,
            entity_type="aggregator",
            entity_key=report.aggregator_uuid,
            deployment_id=None,
            detail={"mac": mac, "source": report.source},
        )
        return IdentityResolution(IdentityOutcome.PROVISIONING_REQUIRED, alert=alert)

    listener = db.get(Listener, mac)
    if listener is None:
        return IdentityResolution(IdentityOutcome.UNKNOWN_MAC)

    if listener.aggregator_id != reporting.id:
        quarantined = quarantine_report(db, report, "mac_conflict")
        alert = _open_alert(
            db,
            alert_type=ALERT_DUPLICATE_IDENTITY,
            entity_type="listener",
            entity_key=mac,
            deployment_id=listener.deployment_id,
            detail={"reason": "mac_conflict", "reported_by": report.aggregator_uuid},
        )
        return IdentityResolution(
            IdentityOutcome.MAC_CONFLICT, listener=listener, quarantined=quarantined, alert=alert
        )

    if report.name is not None and report.name != listener.name:
        quarantined = quarantine_report(db, report, "name_conflict")
        alert = _open_alert(
            db,
            alert_type=ALERT_DUPLICATE_IDENTITY,
            entity_type="listener",
            entity_key=mac,
            deployment_id=listener.deployment_id,
            detail={
                "reason": "name_conflict",
                "inventory_name": listener.name,
                "reported_name": report.name,
            },
        )
        return IdentityResolution(
            IdentityOutcome.NAME_CONFLICT, listener=listener, quarantined=quarantined, alert=alert
        )

    return IdentityResolution(IdentityOutcome.MATCHED, listener=listener)
