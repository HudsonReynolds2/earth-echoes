"""Delivering service settings to devices that arrived late (task E5.7b;
spec 16.4).

Spec 16.4 requires the platform to publish a deployment's service settings to
an Aggregator "as soon as the device exists in inventory". `PUT
/deployments/{id}/services` covers the devices that existed when the operator
saved. This covers the other order — **the Aggregator created after the
services were configured**, which is the normal order for a growing
deployment and the one a cold-start device is in.

## Why it is the same code path and not a special case

The sweep recomputes each deployment's projection and runs it through
`build_change_plan` / `apply_change_plan`, exactly as the services save does.
That is not laziness, it is what makes the sweep safe to run on a timer:
E5.7a's `changed_keys` fix means a device whose snapshot already matches
produces `no_op` and no revision, so a pass over an unchanged fleet writes
nothing and publishes nothing. A bespoke "find devices with no service keys"
query would be a second definition of up-to-date, and the two would disagree
the first time a projection changed shape.

## Why it lives here rather than in `app/controlplane/`

`ReconciliationWorker`'s sweep runner is E3-owned and the phase document
authorizes exactly two discretionary edits to it (section 2). Keeping the
sweep's BODY in `app/services/` means the E3-owned diff is a registration and
nothing else — the same discipline `app/services/status.py` used when it
shipped its re-check sweep as a callable rather than wiring itself in.

## One transaction per deployment

`runner.py`'s own rule for its sweeps: a pass that fails halfway must leave the
deployments it already handled written, and one that committed at the end would
hold locks across every broker round trip. A deployment with no service rows is
skipped rather than written to — touching every untouched deployment on every
cycle is how a sweep becomes the reason a database is busy.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.overrides import get_overrides
from app.config.plan import (
    PlanError,
    apply_change_plan,
    build_change_plan,
    restricted_keys,
)
from app.config.selection import MatchedEntity
from app.controlplane.publisher import DesiredPublisher, publish_all
from app.models import ConfigRevision, Deployment, DeploymentService
from app.secrets import SecretStore
from app.services.projection import GENERATION_KEY, service_settings
from app.services.store import load_services

logger = logging.getLogger(__name__)


@dataclass
class ServiceConfigSweepReport:
    """What one pass did. Shaped like `runner.py`'s own sweep reports, so a
    third entry on that loop reads like the two already there."""

    deployments: int = 0
    revisions: int = 0
    published: int = 0
    failures: int = 0

    @property
    def changed(self) -> bool:
        """Whether this pass is worth a log line. A sweep that found every
        device already up to date is the normal case and says nothing."""
        return bool(self.revisions or self.failures)

    def __str__(self) -> str:
        return (
            f"{self.deployments} deployment(s), {self.revisions} revision(s), "
            f"{self.published} published, {self.failures} failed"
        )


def _deployments_with_services(session_factory: sessionmaker[Session]) -> list[uuid.UUID]:
    with session_factory() as db:
        return list(
            db.scalars(
                select(DeploymentService.deployment_id)
                .distinct()
                .order_by(DeploymentService.deployment_id)
            )
        )


def _plan_one(db: Session, secret_store: SecretStore, deployment_id: uuid.UUID) -> list[uuid.UUID]:
    """Stage this deployment's projection and return the revision ids minted.

    `actor_user_id=None` on purpose: nobody clicked anything. The audit trail
    for a sweep-minted revision is the `reconciliation_event` row its state
    transition writes (E3.11), which is where an automatic action belongs —
    attributing it to the operator who last saved the services would be a
    small lie that a timeline reader would eventually act on.
    """
    projection = service_settings(load_services(db, deployment_id), secret_store.get)
    if not projection and not (
        restricted_keys() & set(get_overrides(db, "deployment", str(deployment_id)))
    ):
        # **Nothing to deliver and nothing to withdraw, so do not build a plan.**
        # `build_change_plan` loads every hierarchy table and recomputes
        # effective config for every device under the deployment; on a 20 x 30
        # fleet that is 620 merges, and this sweep runs once a minute. The very
        # common case that hits it is a deployment with only its `mqtt` row
        # configured — which is EVERY deployment with a working control plane
        # and no telemetry services yet, i.e. every deployment between E3 and
        # the operator finishing the S5 wizard.
        return []
    # **The generation is added AFTER the early return, and the order is the
    # whole point (D139).** `service_settings` omits the key when no generation
    # is passed, so a projection built without it stops asserting a value and
    # the effective config falls back to the catalog default of 0 — which this
    # sweep then delivers as a revision. Omitted, a once-a-minute sweep reset
    # every rotated deployment's counter from N back to 0 and minted a revision
    # to publish the reset, destroying the one signal a rotation gives a device
    # (D134) within a minute of the rotation.
    #
    # But it cannot be passed to the call ABOVE either: the counter is always
    # present, so the projection would never be empty, the "nothing to deliver"
    # return would be unreachable, and every deployment holding only an `mqtt`
    # row would rebuild a full change plan every minute — the exact cost that
    # return exists to avoid. A deployment with nothing else to deliver has no
    # generated stack and therefore no rotation to announce, so riding along
    # with a projection that already has content is both correct and cheap.
    deployment = db.get(Deployment, deployment_id)
    if deployment is not None:
        projection[GENERATION_KEY] = deployment.services_credentials_generation
    matched = [
        MatchedEntity(
            entity_type="deployment",
            entity_id=str(deployment_id),
            name="",
            deployment_id=str(deployment_id),
            tags=(),
        )
    ]
    plan = build_change_plan(db, matched, projection, "deployment", [], allow_write_restricted=True)
    revisions, _ = apply_change_plan(
        db, secret_store, plan, projection, None, allow_write_restricted=True
    )
    return [revision.id for revision in revisions]


async def service_config_sweep(
    session_factory: sessionmaker[Session],
    secret_store: SecretStore,
    publisher: DesiredPublisher | None,
    *,
    publish_enabled: bool,
) -> ServiceConfigSweepReport:
    """One pass: every deployment's service settings, delivered to every device
    that does not already have them.

    Async because delivery is the point — a revision minted and not published
    leaves the device exactly as uninformed as before, and spec 16.4 is about
    the device receiving the settings rather than about a row existing.

    A deployment that raises is logged and counted; the pass continues. One
    deployment with a broken projection must not stop every other deployment's
    devices from being told.
    """
    report = ServiceConfigSweepReport()
    for deployment_id in _deployments_with_services(session_factory):
        report.deployments += 1
        try:
            with session_factory() as db:
                minted = _plan_one(db, secret_store, deployment_id)
                db.commit()
        except PlanError:
            logger.exception(
                "the service-settings projection for deployment %s is invalid; skipping it",
                deployment_id,
            )
            report.failures += 1
            continue
        except Exception:
            logger.exception("the service-settings sweep failed for deployment %s", deployment_id)
            report.failures += 1
            continue
        if not minted:
            continue
        report.revisions += len(minted)
        # Reloaded rather than carried across the session boundary: the rows
        # above belong to a session that has closed, and `publish_all` opens
        # its own per-revision transaction anyway (E3.4's rule).
        with session_factory() as db:
            rows = list(db.scalars(select(ConfigRevision).where(ConfigRevision.id.in_(minted))))
            published = await publish_all(
                session_factory,
                publisher,
                rows,
                publish_enabled=publish_enabled,
                actor_user_id=None,
            )
        report.published += len(published)
    return report
