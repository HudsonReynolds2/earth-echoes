"""Row access for `deployment_service` (task E5.1; spec 16.2).

Stage-never-commit, exactly like `app.config.overrides`: the caller owns the
transaction. Secrets follow the same D51 split - `delete_services_for`
RETURNS the SecretStore names it orphaned rather than deleting them, because
deleting first would lose the plaintext if the caller's transaction rolled
back, and the caller runs them only AFTER its commit.

**Nothing here writes the status columns.** `status`, `status_reason`,
`last_tested_at`, `consecutive_failures` and `last_test_detail` are E5.5's
lifecycle (`app/services/status.py::apply_test_results`), fed by E5.3's
testers; E5.1 creates the columns and their `untested` / `0` defaults and
stops there. An upsert leaving a verified service's status alone is
deliberate: E5.5, not this module, decides what re-entering credentials does
to a verdict.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SERVICE_KEYS, DeploymentService


def load_service(
    db: Session, deployment_id: uuid.UUID, service_key: str
) -> DeploymentService | None:
    """One deployment's row for one service, or None if it has none."""
    return db.scalar(
        select(DeploymentService).where(
            DeploymentService.deployment_id == deployment_id,
            DeploymentService.service_key == service_key,
        )
    )


def load_services(db: Session, deployment_id: uuid.UUID) -> list[DeploymentService]:
    """Every service row for one deployment, in the spec 16.2 table's order.

    Ordered by the vocabulary rather than alphabetically so the wizard, the
    test runner and the status rollup all present the five services in the
    order the spec lists them, without each sorting for itself.
    """
    rows = list(
        db.scalars(
            select(DeploymentService).where(DeploymentService.deployment_id == deployment_id)
        )
    )
    order = {key: index for index, key in enumerate(SERVICE_KEYS)}
    return sorted(rows, key=lambda row: (order.get(row.service_key, len(order)), row.service_key))


def upsert_service(
    db: Session,
    deployment_id: uuid.UUID,
    service_key: str,
    *,
    config: Mapping[str, Any] | None = None,
    secret_names: Mapping[str, str] | None = None,
    host: str | None = None,
    port: int | None = None,
    tls_enabled: bool = True,
    ca_cert_pem: str | None = None,
    username: str | None = None,
    password_secret_name: str | None = None,
) -> DeploymentService:
    """Create or WHOLESALE REPLACE one service row's configuration.

    PUT is never a merge (the E1.7 tags precedent, and `put_overrides`'):
    every field the caller omits is written as its empty value, so a save that
    drops a field really drops it rather than leaving a stale value behind
    that nothing in the UI can see. Callers that mean "keep what is stored"
    read the row first - E5.2's keep sentinel is the wire shape for that.

    `secret_names` maps a field name to a SecretStore name. Putting the
    plaintext there is the one thing this module cannot check for you; it
    holds names, and rule R2 makes that the whole point of the column.
    """
    if service_key not in SERVICE_KEYS:
        raise ValueError(
            f"unknown service_key {service_key!r}; the spec 16.2 services are {SERVICE_KEYS}"
        )
    row = load_service(db, deployment_id, service_key)
    if row is None:
        row = DeploymentService(deployment_id=deployment_id, service_key=service_key)
        db.add(row)
    row.config = dict(config or {})
    row.secret_names = dict(secret_names or {})
    row.host = host
    row.port = port
    row.tls_enabled = tls_enabled
    row.ca_cert_pem = ca_cert_pem
    row.username = username
    row.password_secret_name = password_secret_name
    return row


def delete_services_for(db: Session, deployment_id: uuid.UUID) -> tuple[str, ...]:
    """Deployment-deletion cleanup, wired into `DELETE /deployments/{id}`.

    Removes every service row and returns every SecretStore name those rows
    named - each row's `password_secret_name` plus every value in its
    `secret_names` map - for the caller's POST-COMMIT deletion (D51). Without
    this the delete endpoint hits `deployment_service`'s real foreign key and
    500s, because `devbroker.register_services` writes an `mqtt` row for every
    deployment. Deletion rather than a refusal is the right answer for the
    same reason `delete_overrides_for` is: phase 1's "no cascade deletes"
    choice is about entities in the hierarchy, and a service row is
    infrastructure attached to the deployment, not a child of it.
    """
    names: dict[str, None] = {}
    for row in load_services(db, deployment_id):
        if row.password_secret_name:
            names.setdefault(row.password_secret_name, None)
        for name in row.secret_names.values():
            names.setdefault(name, None)
        db.delete(row)
    return tuple(names)
