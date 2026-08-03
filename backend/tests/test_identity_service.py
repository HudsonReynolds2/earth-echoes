"""Gate 24: E1.5 report-time identity services (spec 4.3 items 2-3; D37).

Table-driven over the five outcomes, with the load-bearing invariants proven
directly: conflicts NEVER touch inventory rows (byte-identical reload),
quarantine appends while alerts dedupe on the open row, a resolved alert
permits a fresh one, membership is checked against inventory rather than a
sentinel, and the partial unique index holds under raw inserts.
"""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from test_auth import pg_url  # noqa: F401  (module fixture reuse)

from app.db import create_session_factory
from app.inventory.identity import (
    IdentityOutcome,
    ProvisioningRequiredError,
    ReportedIdentity,
    check_aggregator_membership,
    handle_reported_identity,
    require_known_aggregator,
)
from app.models import (
    Aggregator,
    Deployment,
    InventoryAlert,
    Listener,
    Organization,
    Pod,
    QuarantinedReport,
)

pytestmark = pytest.mark.integration

MAC = "02:E1:05:00:00:01"


@pytest.fixture(scope="module")
def factory(pg_url):  # noqa: F811
    _, session_factory = create_session_factory(pg_url)
    return session_factory


@pytest.fixture(scope="module")
def world(factory):
    """One deployment, two pods with aggregators, one registered listener."""
    with factory() as db:
        org = Organization(name="ident-org")
        db.add(org)
        db.flush()
        dep = Deployment(organization_id=org.id, name="ident-dep", slug="ident-dep")
        db.add(dep)
        db.flush()
        pod_1 = Pod(deployment_id=dep.id, name="ident-pod-1")
        pod_2 = Pod(deployment_id=dep.id, name="ident-pod-2")
        db.add_all([pod_1, pod_2])
        db.flush()
        agg_1 = Aggregator(pod_id=pod_1.id, aggregator_uuid="ident-agg-1")
        agg_2 = Aggregator(pod_id=pod_2.id, aggregator_uuid="ident-agg-2")
        db.add_all([agg_1, agg_2])
        db.flush()
        db.add(
            Listener(mac=MAC, name="ident-listener", aggregator_id=agg_1.id, deployment_id=dep.id)
        )
        db.commit()
        return {"dep": dep.id, "agg_1": agg_1.id}


def _snapshot(db, mac: str) -> dict[str, Any]:
    row = db.get(Listener, mac)
    assert row is not None
    return {column.name: getattr(row, column.name) for column in Listener.__table__.columns}


def _counts(db) -> tuple[int, int]:
    quarantined = db.scalar(select(func.count()).select_from(QuarantinedReport)) or 0
    alerts = db.scalar(select(func.count()).select_from(InventoryAlert)) or 0
    return quarantined, alerts


def test_clean_match_touches_nothing(factory, world):
    with factory() as db:
        before = _snapshot(db, MAC)
        q0, a0 = _counts(db)
        result = handle_reported_identity(
            db, ReportedIdentity(mac=MAC, aggregator_uuid="ident-agg-1", name="ident-listener")
        )
        db.commit()
        assert result.outcome is IdentityOutcome.MATCHED
        assert result.listener is not None and result.listener.mac == MAC
        assert _snapshot(db, MAC) == before
        assert _counts(db) == (q0, a0)


def test_name_conflict_quarantines_and_never_touches_inventory(factory, world):
    with factory() as db:
        before = _snapshot(db, MAC)
        result = handle_reported_identity(
            db,
            ReportedIdentity(
                mac=MAC,
                aggregator_uuid="ident-agg-1",
                name="impostor-name",
                raw={"rssi": -70},
            ),
        )
        db.commit()
        assert result.outcome is IdentityOutcome.NAME_CONFLICT
        assert result.quarantined is not None and result.quarantined.reason == "name_conflict"
        assert result.quarantined.report["rssi"] == -70
        assert result.alert is not None and result.alert.alert_type == "duplicate_identity"
        assert result.alert.deployment_id == world["dep"]
        # The inventory row is byte-identical - spec 4.3's core promise.
        assert _snapshot(db, MAC) == before


def test_mac_conflict_from_a_different_aggregator(factory, world):
    with factory() as db:
        before = _snapshot(db, MAC)
        result = handle_reported_identity(
            db,
            ReportedIdentity(mac=MAC, aggregator_uuid="ident-agg-2", name="ident-listener"),
        )
        db.commit()
        assert result.outcome is IdentityOutcome.MAC_CONFLICT
        assert result.quarantined is not None and result.quarantined.reason == "mac_conflict"
        assert _snapshot(db, MAC) == before


def test_unknown_aggregator_raises_provisioning_required_alert(factory, world):
    with factory() as db:
        result = handle_reported_identity(
            db, ReportedIdentity(mac="02:E1:05:00:00:99", aggregator_uuid="never-provisioned")
        )
        db.commit()
        assert result.outcome is IdentityOutcome.PROVISIONING_REQUIRED
        assert result.alert is not None
        assert result.alert.alert_type == "provisioning_required"
        assert result.quarantined is None  # no identity conflict, just unknown reporter
        # Membership, not sentinel: the check is a lookup that found nothing.
        assert check_aggregator_membership(db, "never-provisioned") is None


def test_unknown_mac_from_known_aggregator_has_zero_side_effects(factory, world):
    with factory() as db:
        q0, a0 = _counts(db)
        result = handle_reported_identity(
            db, ReportedIdentity(mac="02:E1:05:00:00:42", aggregator_uuid="ident-agg-1")
        )
        db.commit()
        assert result.outcome is IdentityOutcome.UNKNOWN_MAC
        assert result.listener is None and result.alert is None and result.quarantined is None
        assert _counts(db) == (q0, a0)


def test_quarantine_appends_while_open_alerts_dedupe(factory, world):
    with factory() as db:
        q0, a0 = _counts(db)
        first = handle_reported_identity(
            db, ReportedIdentity(mac=MAC, aggregator_uuid="ident-agg-1", name="impostor-2")
        )
        db.commit()
        second = handle_reported_identity(
            db, ReportedIdentity(mac=MAC, aggregator_uuid="ident-agg-1", name="impostor-3")
        )
        db.commit()
        q1, a1 = _counts(db)
        assert q1 == q0 + 2  # every report is evidence
        # The duplicate_identity alert for this MAC was opened by an earlier
        # test and stays deduped: no new alert row at all.
        assert a1 == a0
        assert first.alert is not None and second.alert is not None
        assert first.alert.id == second.alert.id


def test_resolving_the_alert_allows_a_fresh_one(factory, world):
    from app.models import utcnow

    with factory() as db:
        open_alert = db.scalars(
            select(InventoryAlert).where(
                InventoryAlert.alert_type == "duplicate_identity",
                InventoryAlert.entity_key == MAC,
                InventoryAlert.resolved_at.is_(None),
            )
        ).one()
        open_alert.resolved_at = utcnow()
        db.commit()
        result = handle_reported_identity(
            db, ReportedIdentity(mac=MAC, aggregator_uuid="ident-agg-1", name="impostor-4")
        )
        db.commit()
        assert result.alert is not None and result.alert.id != open_alert.id


def test_partial_unique_index_blocks_a_second_open_alert(factory, world):
    with factory() as db:
        db.add(
            InventoryAlert(
                alert_type="duplicate_identity",
                entity_type="listener",
                entity_key=MAC,  # an OPEN alert exists again after the fresh one above
                deployment_id=None,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_require_known_aggregator_raises_and_alerts(factory, world):
    with factory() as db:
        aggregator = require_known_aggregator(db, "ident-agg-1")
        assert aggregator.id == world["agg_1"]
        with pytest.raises(ProvisioningRequiredError):
            require_known_aggregator(db, "still-not-provisioned")
        db.commit()
        alert = db.scalars(
            select(InventoryAlert).where(
                InventoryAlert.entity_key == "still-not-provisioned",
                InventoryAlert.resolved_at.is_(None),
            )
        ).first()
        assert alert is not None and alert.alert_type == "provisioning_required"


def test_service_stages_but_never_commits(factory, world):
    """The record_audit convention: an uncommitted session leaves nothing."""
    with factory() as db:
        q0, a0 = _counts(db)
        handle_reported_identity(
            db, ReportedIdentity(mac=MAC, aggregator_uuid="ident-agg-2", name="rollback-probe")
        )
        db.rollback()
    with factory() as db:
        assert _counts(db) == (q0, a0)
