"""Open alerts, counted per device and per Deployment (task E6.4; spec 9.2, 9.3; D191).

**This is the seam E7 extends.** Today the only alerts are E1's open
`inventory_alert` rows. E7 adds Grafana alerts by adding them to these same
three counts. The status derivation (`device_status.with_alerts`) and the map
rollup only ever see the numbers, so neither changes when the source does.

The attachment rule (phase-6 fixed choice 4):

- a `listener` alert whose `entity_key` is a MAC in inventory attaches to that
  Listener;
- an `aggregator` alert whose `entity_key` is an `aggregator_uuid` in inventory
  attaches to that Aggregator;
- anything else (in practice a `provisioning_required` for an Aggregator
  nobody has entered, which by definition is not in inventory) is UNATTACHED.
  It counts toward its Deployment's badge when it names a Deployment, and
  toward nothing when it does not. It never changes a marker's status,
  because there is no device to be `alerting`.

Every function is one query, whatever the number of devices.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Aggregator, InventoryAlert, Listener

_OPEN = InventoryAlert.resolved_at.is_(None)


def listener_alert_counts(db: Session, macs: list[str]) -> dict[str, int]:
    """Open alerts attached to each of these Listeners."""
    if not macs:
        return {}
    rows = db.execute(
        select(InventoryAlert.entity_key, func.count())
        .where(_OPEN, InventoryAlert.entity_type == "listener", InventoryAlert.entity_key.in_(macs))
        .group_by(InventoryAlert.entity_key)
    ).all()
    return {str(mac): int(count) for mac, count in rows}


def aggregator_alert_counts(db: Session, aggregator_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Open alerts attached to each of these Aggregators, matched through
    `aggregator_uuid`, which is what an alert names."""
    if not aggregator_ids:
        return {}
    rows = db.execute(
        select(Aggregator.id, func.count())
        .join(InventoryAlert, InventoryAlert.entity_key == Aggregator.aggregator_uuid)
        .where(_OPEN, InventoryAlert.entity_type == "aggregator", Aggregator.id.in_(aggregator_ids))
        .group_by(Aggregator.id)
    ).all()
    return {agg_id: int(count) for agg_id, count in rows}


def unattached_alert_counts(db: Session, deployment_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Open alerts naming one of these Deployments but no device in inventory."""
    if not deployment_ids:
        return {}
    known_listener = select(Listener.mac).where(Listener.mac == InventoryAlert.entity_key)
    known_aggregator = select(Aggregator.id).where(
        Aggregator.aggregator_uuid == InventoryAlert.entity_key
    )
    rows = db.execute(
        select(InventoryAlert.deployment_id, func.count())
        .where(
            _OPEN,
            InventoryAlert.deployment_id.in_(deployment_ids),
            ~(
                (InventoryAlert.entity_type == "listener") & known_listener.exists()
                | (InventoryAlert.entity_type == "aggregator") & known_aggregator.exists()
            ),
        )
        .group_by(InventoryAlert.deployment_id)
    ).all()
    return {dep_id: int(count) for dep_id, count in rows if dep_id is not None}
