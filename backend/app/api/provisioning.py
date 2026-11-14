"""The provisioning bundle endpoints (task E4.8; spec 8.3, 8.5, 13; D178).

- `POST /provisioning/bundles`: generate (MANAGE_PROVISIONING, audited).
- `GET /provisioning/bundles`: the D7 list envelope, scoped (VIEW_PROVISIONING).
- `GET /provisioning/bundles/{id}`: one bundle and its records (VIEW_PROVISIONING).
- `GET /provisioning/bundles/{id}/download`: the stored archive
  (**MANAGE_PROVISIONING**, audited).
- `GET /provisioning/records/{id}` (VIEW_PROVISIONING) and
  `PATCH /provisioning/records/{id}` (MANAGE_PROVISIONING, audited): spec 13's
  record surface. The PATCH accepts exactly one transition, `flashed`, because
  it is the only status a person can assert (E4.9, D179). Every other status is
  the reconciler's.

**Download is MANAGE, not VIEW** (D178), on the E5 stack-download precedent:
the archive carries every Aggregator's broker login in usable plaintext (spec
16.4), so a viewer may see that a bundle exists and what it covers, and only a
role that may generate bundles may take one away.

Out-of-scope and missing bundles answer the same 404 (D35), so a bundle id is
never an existence oracle. No response from this router carries a secret: the
bundle summary and its records are metadata, and the archive bytes leave only
through the download.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from app.api.hierarchy_common import not_found
from app.api.pagination import ListResponse, PageParams, apply_page
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, has_permission
from app.errors import AppError
from app.inventory.naming import normalize_mac
from app.models import DeviceProvisioningRecord, ProvisioningBundle, UserSession
from app.provisioning.bootstrap import BootstrapRefused
from app.provisioning.generate import GenerationError
from app.provisioning.records import RecordTransitionError, mark_flashed, unmark_flashed
from app.provisioning.service import ArchiveMissing, PodChoice, create_bundle, load_archive
from app.scoping import require_any_assignment, scope_filter, visible_deployments
from app.services.credentials import CredentialError

router = APIRouter(prefix="/provisioning", tags=["provisioning"])

SORTABLE = {"generated_at": ProvisioningBundle.generated_at}


class PodChoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pod_id: uuid.UUID
    mode: Literal["per_pod", "per_listener", "mixed"] = "per_pod"
    listener_macs: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("listener_macs")
    @classmethod
    def _canonical(cls, value: list[str]) -> list[str]:
        return [normalize_mac(mac) for mac in value]


class CreateBundleIn(BaseModel):
    """Spec 13: "body names the target and the mode". `scope="pod"` takes
    exactly one entry in `pods`. `scope="deployment"` covers every Pod with an
    Aggregator, and `pods` overrides the per-Pod default of `per_pod`."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: uuid.UUID
    scope: Literal["pod", "deployment"]
    pods: list[PodChoiceIn] = Field(default_factory=list, max_length=500)


class RecordOut(BaseModel):
    id: uuid.UUID
    target_type: str
    target_id: str
    pod_id: uuid.UUID
    aggregator_uuid: str
    file_kind: str
    file_path: str
    config_revision_id: uuid.UUID | None
    config_checksum: str | None
    status: str
    status_history: list[dict[str, object]]


class BundleOut(BaseModel):
    id: uuid.UUID
    deployment_id: uuid.UUID
    scope_type: str
    scope_id: uuid.UUID
    plan: dict[str, object]
    template_version: int
    firmware_key_version: int
    archive_checksum: str
    generated_by: uuid.UUID | None
    generated_at: datetime
    record_count: int


class BundleDetailOut(BundleOut):
    records: list[RecordOut]


class BundlesQuery(PageParams):
    deployment_id: uuid.UUID | None = None


def _bundle_out(bundle: ProvisioningBundle, record_count: int) -> BundleOut:
    return BundleOut(
        id=bundle.id,
        deployment_id=bundle.deployment_id,
        scope_type=bundle.scope_type,
        scope_id=bundle.scope_id,
        plan=bundle.plan,
        template_version=bundle.template_version,
        firmware_key_version=bundle.firmware_key_version,
        archive_checksum=bundle.archive_checksum,
        generated_by=bundle.generated_by,
        generated_at=bundle.generated_at,
        record_count=record_count,
    )


def _record_out(record: DeviceProvisioningRecord) -> RecordOut:
    return RecordOut(
        id=record.id,
        target_type=record.target_type,
        target_id=record.target_id,
        pod_id=record.pod_id,
        aggregator_uuid=record.aggregator_uuid,
        file_kind=record.file_kind,
        file_path=record.file_path,
        config_revision_id=record.config_revision_id,
        config_checksum=record.config_checksum,
        status=record.status,
        status_history=list(record.status_history),
    )


def _records(db: DbDep, bundle_id: uuid.UUID) -> list[DeviceProvisioningRecord]:
    return list(
        db.scalars(
            select(DeviceProvisioningRecord)
            .where(DeviceProvisioningRecord.bundle_id == bundle_id)
            .order_by(DeviceProvisioningRecord.file_path, DeviceProvisioningRecord.target_id)
        )
    )


def _resolve(
    db: DbDep, session: UserSession, bundle_id: uuid.UUID, permission: Permission
) -> ProvisioningBundle:
    bundle = db.get(ProvisioningBundle, bundle_id)
    if bundle is None or not has_permission(
        session.user.role_assignments, permission, bundle.deployment_id
    ):
        raise not_found("bundle")
    return bundle


@router.post("/bundles", response_model=BundleDetailOut, status_code=201)
async def post_bundle(
    body: CreateBundleIn,
    request: Request,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> BundleDetailOut:
    if not has_permission(
        actor.user.role_assignments, Permission.MANAGE_PROVISIONING, body.deployment_id
    ):
        # The D35 rule: a deployment the caller cannot provision is reported
        # the same way as one that does not exist.
        raise not_found("deployment")
    state = request.app.state
    try:
        bundle, records = await create_bundle(
            db,
            state.session_factory,
            state.secret_store,
            state.credential_provider,
            state.firmware_key,
            deployment_id=body.deployment_id,
            scope=body.scope,
            choices=[
                PodChoice(pod_id=p.pod_id, mode=p.mode, listener_macs=frozenset(p.listener_macs))
                for p in body.pods
            ],
            include_credentials=state.settings.bootstrap_credentials,
            actor_user_id=actor.user_id,
        )
    except LookupError as error:
        db.rollback()
        raise AppError("not_found", str(error), status_code=404) from error
    except GenerationError as error:
        db.rollback()
        raise AppError(
            "validation_error", str(error), status_code=422, detail={"missing": list(error.missing)}
        ) from error
    except BootstrapRefused as error:
        db.rollback()
        raise AppError(
            "validation_error",
            str(error),
            status_code=422,
            detail={"deployment": error.deployment_name},
        ) from error
    except CredentialError as error:
        db.rollback()
        raise AppError(
            "service_unavailable",
            f"could not obtain broker credentials: {error}",
            status_code=503,
        ) from error

    record_audit(
        db,
        action="provisioning.bundle.generate",
        entity_type="provisioning_bundle",
        entity_id=str(bundle.id),
        actor_user_id=actor.user_id,
        scope=bundle.deployment_id,
        detail={"scope": bundle.scope_type, "plan": bundle.plan, "records": len(records)},
    )
    db.commit()
    return BundleDetailOut(
        **_bundle_out(bundle, len(records)).model_dump(),
        records=[_record_out(record) for record in records],
    )


@router.get("/bundles", response_model=ListResponse[BundleOut])
def list_bundles(
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    query: Annotated[BundlesQuery, Query()],
) -> ListResponse[BundleOut]:
    statement = select(ProvisioningBundle)
    if query.deployment_id is not None:
        statement = statement.where(ProvisioningBundle.deployment_id == query.deployment_id)
    scope = visible_deployments(session.user.role_assignments, Permission.VIEW_PROVISIONING)
    statement = scope_filter(statement, ProvisioningBundle.deployment_id, scope)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(
        limit=query.limit, offset=query.offset, sort=query.sort or "-generated_at"
    )
    bundles = list(db.scalars(apply_page(statement, windowed, SORTABLE)))
    counts: dict[uuid.UUID, int] = {
        bundle_id: count
        for bundle_id, count in db.execute(
            select(DeviceProvisioningRecord.bundle_id, func.count())
            .where(DeviceProvisioningRecord.bundle_id.in_([b.id for b in bundles]))
            .group_by(DeviceProvisioningRecord.bundle_id)
        ).tuples()
    }
    return ListResponse(
        items=[_bundle_out(b, counts.get(b.id, 0)) for b in bundles],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@router.get("/bundles/{bundle_id}", response_model=BundleDetailOut)
def get_bundle(
    bundle_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> BundleDetailOut:
    bundle = _resolve(db, session, bundle_id, Permission.VIEW_PROVISIONING)
    records = _records(db, bundle.id)
    return BundleDetailOut(
        **_bundle_out(bundle, len(records)).model_dump(),
        records=[_record_out(record) for record in records],
    )


@router.get("/bundles/{bundle_id}/download")
def download_bundle(
    bundle_id: uuid.UUID,
    request: Request,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> Response:
    bundle = _resolve(db, session, bundle_id, Permission.MANAGE_PROVISIONING)
    try:
        archive = load_archive(request.app.state.secret_store, bundle)
    except ArchiveMissing as error:
        raise AppError("internal_error", str(error), status_code=500) from error
    record_audit(
        db,
        action="provisioning.bundle.download",
        entity_type="provisioning_bundle",
        entity_id=str(bundle.id),
        actor_user_id=session.user_id,
        scope=bundle.deployment_id,
        detail={"bytes": len(archive)},
    )
    db.commit()
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="echoes-provisioning-{bundle.id}.zip"'
        },
    )


class PatchRecordIn(BaseModel):
    """Spec 13 `PATCH /provisioning/records/{id}`. Two allowed values, on
    purpose: `flashed` marks a generated card written, and `generated`
    unmarks a flashed one (a mis-click, D187). `registered`, `confirmed` and
    `mismatch` are statements about evidence, and the reconciler derives them
    (D179)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["flashed", "generated"]


def _resolve_record(
    db: DbDep, session: UserSession, record_id: uuid.UUID, permission: Permission
) -> DeviceProvisioningRecord:
    record = db.get(DeviceProvisioningRecord, record_id)
    if record is None or not has_permission(
        session.user.role_assignments, permission, record.deployment_id
    ):
        raise not_found("record")
    return record


@router.get("/records/{record_id}", response_model=RecordOut)
def get_record(
    record_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> RecordOut:
    return _record_out(_resolve_record(db, session, record_id, Permission.VIEW_PROVISIONING))


@router.patch("/records/{record_id}", response_model=RecordOut)
def patch_record(
    record_id: uuid.UUID,
    body: PatchRecordIn,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> RecordOut:
    record = _resolve_record(db, actor, record_id, Permission.MANAGE_PROVISIONING)
    try:
        transition = mark_flashed if body.status == "flashed" else unmark_flashed
        changed = transition(record, actor.user_id)
    except RecordTransitionError as error:
        raise AppError("conflict", str(error), status_code=409) from error
    if changed:
        record_audit(
            db,
            action=(
                "provisioning.record.flashed"
                if body.status == "flashed"
                else "provisioning.record.unflashed"
            ),
            entity_type="device_provisioning_record",
            entity_id=str(record.id),
            actor_user_id=actor.user_id,
            scope=record.deployment_id,
            detail={"target_type": record.target_type, "target_id": record.target_id},
        )
        db.commit()
    return _record_out(record)
