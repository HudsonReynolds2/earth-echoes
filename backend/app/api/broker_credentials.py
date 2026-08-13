"""Per-device broker credential endpoints (task E5.6; spec 7.1, 16.4, 13).

Three routes on one Aggregator's broker login:

* **`POST /aggregators/{id}/broker-credential`** — mint or rotate. Idempotent
  by construction (`DynsecCredentialProvider` deletes and recreates), so a
  retry after a timeout is safe and a rotation is the same call as a first
  mint.
* **`DELETE /aggregators/{id}/broker-credential`** — revoke without deleting
  the device, which is what an operator does when a Pi is stolen or a password
  is believed leaked.
* **`GET`** — the state, for the S5 wizard and E4's provisioning board.

**The response never carries the password, and cannot.** `BrokerCredentialOut`
has no field for it: the plaintext goes from the provider into SecretStore and
nothing returns it, exactly as the services API's write-only rule works (E5.2).
E4.6 reads it back from SecretStore when it writes the bootstrap block, which
is the one place a device credential is ever rendered — into a file the
operator carries to the device.

**`MANAGE_SERVICES` to mint and revoke, `VIEW_SERVICES` to read.** Fixed choice
9 put service credentials with Owner and Deployment Operator, and a broker
login is a grant on the deployment's broker rather than a property of the
inventory row. Note that revocation ALSO happens implicitly under
`MANAGE_DEVICES`, when `DELETE /aggregators/{id}` removes the device — that is
not an inconsistency: destroying a credential is safe in a way that creating
one is not, and a device being decommissioned must never keep a live login
merely because the person decommissioning it held the narrower permission.

**Every refusal is a 404**, following the E1.2 item-route pattern that
`app/api/aggregators.py::_resolve` established: a caller who may not see this
deployment must not be able to tell a real aggregator id from an invented one.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.hierarchy_common import deployment_of_aggregator, not_found
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, has_permission
from app.errors import AppError
from app.models import Aggregator, BrokerCredential, UserSession
from app.scoping import require_any_assignment
from app.services.credentials import (
    BrokerUnreachable,
    CredentialError,
    coordinates_for,
    load_credential,
    mint_credential,
    revoke_credential,
)

router = APIRouter(prefix="/aggregators/{aggregator_id}/broker-credential")


class BrokerCredentialOut(BaseModel):
    """What the platform knows about a device's login. **No password field.**"""

    aggregator_uuid: str
    username: str
    #: minted | revoke_pending | revoked. `revoke_pending` is a real answer and
    #: not an error state: the operator asked, the broker was unreachable, and
    #: the worker's sweep is still trying (D133).
    state: str
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None


def _out(row: BrokerCredential) -> BrokerCredentialOut:
    return BrokerCredentialOut(
        aggregator_uuid=row.aggregator_uuid,
        username=row.username,
        state=row.state,
        created_at=row.created_at,
        updated_at=row.updated_at,
        revoked_at=row.revoked_at,
    )


def _resolve(
    db: DbDep, session: UserSession, aggregator_id: uuid.UUID, permission: Permission
) -> tuple[Aggregator, uuid.UUID]:
    """`aggregators.py::_resolve`, repeated rather than imported.

    Importing it would make this module depend on an E1 route module's private
    helper, and the four lines are the CONVENTION rather than the
    implementation — 404 for missing and 404 for unauthorized, so the two are
    indistinguishable from outside.
    """
    row = db.get(Aggregator, aggregator_id)
    if row is None:
        raise not_found("aggregator")
    deployment_id = deployment_of_aggregator(db, row)
    if not has_permission(session.user.role_assignments, permission, deployment_id):
        raise not_found("aggregator")
    return row, deployment_id


@router.post("", response_model=BrokerCredentialOut, dependencies=[Depends(require_csrf)])
async def mint_broker_credential(
    aggregator_id: uuid.UUID,
    db: DbDep,
    request: Request,
    actor: Annotated[UserSession, Depends(require_any_assignment)],
) -> BrokerCredentialOut:
    """Mint (or rotate) this Aggregator's broker login.

    503 `service_unavailable` when the broker is unreachable — the error
    envelope's existing "a dependency is down, retry" outcome. Unlike
    revocation, minting has no safe degraded path: there is nothing useful to
    record about a credential that was never created.
    """
    aggregator, deployment_id = _resolve(db, actor, aggregator_id, Permission.MANAGE_SERVICES)
    try:
        coordinates = coordinates_for(
            request.app.state.session_factory, request.app.state.secret_store, deployment_id
        )
        row = await mint_credential(
            db,
            request.app.state.secret_store,
            request.app.state.credential_provider,
            coordinates,
            aggregator.aggregator_uuid,
        )
    except BrokerUnreachable as error:
        raise AppError("service_unavailable", str(error), status_code=503) from error
    except CredentialError as error:
        raise AppError("validation_error", str(error), status_code=422) from error

    record_audit(
        db,
        action="broker_credential.mint",
        entity_type="aggregator",
        entity_id=str(aggregator.id),
        actor_user_id=actor.user_id,
        scope=deployment_id,
        # The username is derived from the aggregator_uuid and is not a secret;
        # the password never leaves SecretStore.
        detail={"aggregator_uuid": aggregator.aggregator_uuid, "username": row.username},
    )
    db.commit()
    return _out(row)


@router.delete("", response_model=BrokerCredentialOut, dependencies=[Depends(require_csrf)])
async def revoke_broker_credential(
    aggregator_id: uuid.UUID,
    db: DbDep,
    request: Request,
    actor: Annotated[UserSession, Depends(require_any_assignment)],
) -> BrokerCredentialOut:
    """Destroy this Aggregator's broker login, leaving the device in inventory.

    A broker that will not answer leaves the row `revoke_pending` and returns
    200 rather than 503, and the `state` in the body is what says so. The
    operator's intent is recorded and the sweep finishes the job; a 503 here
    would invite them to click again, which changes nothing.
    """
    aggregator, deployment_id = _resolve(db, actor, aggregator_id, Permission.MANAGE_SERVICES)
    row = load_credential(db, deployment_id, aggregator.aggregator_uuid)
    if row is None:
        raise AppError(
            "not_found", "this aggregator has no minted broker credential", status_code=404
        )
    try:
        coordinates = coordinates_for(
            request.app.state.session_factory, request.app.state.secret_store, deployment_id
        )
        row = await revoke_credential(db, request.app.state.credential_provider, coordinates, row)
    except CredentialError as error:
        raise AppError("validation_error", str(error), status_code=422) from error

    record_audit(
        db,
        action="broker_credential.revoke",
        entity_type="aggregator",
        entity_id=str(aggregator.id),
        actor_user_id=actor.user_id,
        scope=deployment_id,
        detail={"aggregator_uuid": aggregator.aggregator_uuid, "state": row.state},
    )
    db.commit()
    return _out(row)


@router.get("", response_model=BrokerCredentialOut)
def get_broker_credential(
    aggregator_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> BrokerCredentialOut:
    """Whether this device has a credential, and what state it is in.

    `VIEW_SERVICES` because there is no secret here to withhold, and both the
    S5 wizard and E4's provisioning board need to render "this Pi can reach the
    broker" without holding `MANAGE_SERVICES`.
    """
    aggregator, deployment_id = _resolve(db, session, aggregator_id, Permission.VIEW_SERVICES)
    row = load_credential(db, deployment_id, aggregator.aggregator_uuid)
    if row is None:
        raise AppError(
            "not_found", "this aggregator has no minted broker credential", status_code=404
        )
    return _out(row)
