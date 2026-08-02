"""User administration (task E0.9; spec 13): GET/POST /users and
PATCH /users/{id} with role and scope assignment, owner-only.

Every E0 mechanism converges here: require_permission(MANAGE_USERS) gates the
router, mutations add the CSRF dependency and the audit hook, and the list
rides the D7 contract via the UsersQuery-extends-PageParams pattern.
Deactivating a user revokes their live sessions immediately (D1), and the
self-lockout guard stops an owner from cutting off their own access.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.pagination import ListResponse, PageParams, apply_page
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.passwords import hash_password
from app.auth.rbac import Permission, Role, require_permission
from app.errors import AppError
from app.models import Deployment, RoleAssignment, User, UserSession, utcnow

router = APIRouter(
    prefix="/users", dependencies=[Depends(require_permission(Permission.MANAGE_USERS))]
)

SORTABLE = {"email": User.email, "created_at": User.created_at}


class AssignmentBody(BaseModel):
    role: Role
    deployment_id: uuid.UUID | None = None


class AssignmentOut(BaseModel):
    role: str
    deployment_id: uuid.UUID | None


class UserAdminEntry(BaseModel):
    id: uuid.UUID
    email: str
    is_active: bool
    created_at: datetime
    assignments: list[AssignmentOut]


class CreateUserBody(BaseModel):
    email: EmailStr
    password: str
    is_active: bool = True
    assignments: list[AssignmentBody] = []


class UpdateUserBody(BaseModel):
    email: EmailStr | None = None
    password: str | None = None
    is_active: bool | None = None
    assignments: list[AssignmentBody] | None = None


class UsersQuery(PageParams):
    email: str | None = None
    is_active: bool | None = None


def _entry(user: User) -> UserAdminEntry:
    return UserAdminEntry(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        created_at=user.created_at,
        assignments=[
            AssignmentOut(role=a.role, deployment_id=a.deployment_id) for a in user.role_assignments
        ],
    )


def _validate_assignment_scopes(db: DbDep, bodies: list[AssignmentBody]) -> None:
    """Scoped grants must reference a real deployment (E1.1 added the FK,
    DECISIONS D33). Pre-validating turns what would surface as an FK
    IntegrityError - miscaught by the email-conflict handler - into the
    422 the client can act on."""
    wanted = {body.deployment_id for body in bodies if body.deployment_id is not None}
    if not wanted:
        return
    found = set(db.scalars(select(Deployment.id).where(Deployment.id.in_(wanted))))
    missing = wanted - found
    if missing:
        raise AppError(
            "validation_error",
            f"unknown deployment id(s): {', '.join(sorted(str(m) for m in missing))}",
            status_code=422,
        )


def _replace_assignments(db: DbDep, user: User, bodies: list[AssignmentBody]) -> None:
    _validate_assignment_scopes(db, bodies)
    for existing in list(user.role_assignments):
        db.delete(existing)
    db.flush()
    for body in bodies:
        db.add(
            RoleAssignment(user_id=user.id, role=body.role.value, deployment_id=body.deployment_id)
        )
    db.flush()
    db.refresh(user)


@router.get("", response_model=ListResponse[UserAdminEntry])
def list_users(db: DbDep, query: Annotated[UsersQuery, Query()]) -> ListResponse[UserAdminEntry]:
    statement = select(User)
    if query.email is not None:
        statement = statement.where(User.email.icontains(query.email))
    if query.is_active is not None:
        statement = statement.where(User.is_active == query.is_active)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(limit=query.limit, offset=query.offset, sort=query.sort or "email")
    users = db.scalars(apply_page(statement, windowed, SORTABLE)).all()
    return ListResponse(
        items=[_entry(user) for user in users],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@router.post("", response_model=UserAdminEntry, status_code=201)
def create_user(
    body: CreateUserBody, db: DbDep, actor: Annotated[UserSession, Depends(require_csrf)]
) -> UserAdminEntry:
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        is_active=body.is_active,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise AppError(
            "conflict", f"email {body.email!r} already exists", status_code=409
        ) from error
    _replace_assignments(db, user, body.assignments)
    record_audit(
        db,
        action="user.create",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=actor.user_id,
        detail={"email": user.email, "roles": [a.role.value for a in body.assignments]},
    )
    db.commit()
    db.refresh(user)
    return _entry(user)


@router.patch("/{user_id}", response_model=UserAdminEntry)
def update_user(
    user_id: uuid.UUID,
    body: UpdateUserBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> UserAdminEntry:
    user = db.get(User, user_id)
    if user is None:
        raise AppError("not_found", "no such user", status_code=404)

    changed: list[str] = []
    if body.email is not None and body.email != user.email:
        user.email = body.email
        changed.append("email")
    if body.password is not None:
        user.password_hash = hash_password(body.password)
        changed.append("password")

    if body.is_active is not None and body.is_active != user.is_active:
        if user.id == actor.user_id and not body.is_active:
            raise AppError("conflict", "cannot deactivate your own account", status_code=409)
        user.is_active = body.is_active
        changed.append("is_active")
        if not user.is_active:
            # D1: deactivation revokes live sessions immediately.
            for session in db.scalars(
                select(UserSession).where(
                    UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
                )
            ):
                session.revoked_at = utcnow()

    if body.assignments is not None:
        if user.id == actor.user_id and not any(
            a.role == Role.OWNER and a.deployment_id is None for a in body.assignments
        ):
            raise AppError(
                "conflict", "cannot remove your own organization-wide owner role", status_code=409
            )
        _replace_assignments(db, user, body.assignments)
        changed.append("assignments")

    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise AppError("conflict", "email already exists", status_code=409) from error

    record_audit(
        db,
        action="user.update",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=actor.user_id,
        detail={"changed": changed},  # field names only; never values
    )
    db.commit()
    db.refresh(user)
    return _entry(user)
