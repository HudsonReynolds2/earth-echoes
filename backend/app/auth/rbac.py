"""RBAC framework (task E0.7; spec 12.3). One of the four test-critical
components (spec 14.5): backend/tests/test_rbac.py is its contract and no
later session may weaken it.

Roles are fixed by the spec. Permissions are this platform's verbs; later
epics extend the enum and the map deliberately, never ad hoc. An assignment
scoped to a deployment grants within that deployment; a NULL scope grants
organization-wide (docs/INTERFACES.md). The frontend mirrors ROLE_PERMISSIONS
in src/lib/rbac.ts; a parity test keeps the two in lockstep.
"""

import uuid
from collections.abc import Callable, Iterable
from enum import StrEnum

from fastapi import Request

from app.auth.deps import SessionDep
from app.errors import AppError
from app.models import RoleAssignment, UserSession


class Role(StrEnum):
    OWNER = "owner"
    DEPLOYMENT_OPERATOR = "deployment_operator"
    FIELD_TECH = "field_tech"
    VIEWER = "viewer"


class Permission(StrEnum):
    MANAGE_USERS = "manage_users"
    VIEW_AUDIT = "view_audit"
    MANAGE_DEVICES = "manage_devices"
    MANAGE_CONFIG = "manage_config"
    MANAGE_PROVISIONING = "manage_provisioning"
    VIEW_PROVISIONING = "view_provisioning"
    VIEW_TELEMETRY = "view_telemetry"
    VIEW_STATUS = "view_status"
    # E5.2, phase-5 fixed choice 9. A deployment's service credentials are its
    # keys to everything it stores, so MANAGE is Owner and Deployment Operator
    # only - a Field Tech provisions hardware and has no business holding an
    # Influx admin token. VIEW goes to all four roles: the services API is
    # write-only, so a read carries endpoints and status and no secret.
    MANAGE_SERVICES = "manage_services"
    VIEW_SERVICES = "view_services"


# Spec 12.3, verbatim intent: Owner has everything; Deployment Operator manages
# devices, config, reconciliation, and provisioning within assigned deployments
# with telemetry; Field Tech only provisions and tracks (no config push, no
# telemetry depth); Viewer is read-only.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.DEPLOYMENT_OPERATOR: frozenset(
        {
            Permission.MANAGE_DEVICES,
            Permission.MANAGE_CONFIG,
            Permission.MANAGE_PROVISIONING,
            Permission.VIEW_PROVISIONING,
            Permission.VIEW_TELEMETRY,
            Permission.VIEW_STATUS,
            Permission.MANAGE_SERVICES,
            Permission.VIEW_SERVICES,
        }
    ),
    Role.FIELD_TECH: frozenset(
        {
            Permission.MANAGE_PROVISIONING,
            Permission.VIEW_PROVISIONING,
            Permission.VIEW_STATUS,
            Permission.VIEW_SERVICES,
        }
    ),
    Role.VIEWER: frozenset(
        {
            Permission.VIEW_PROVISIONING,
            Permission.VIEW_TELEMETRY,
            Permission.VIEW_STATUS,
            Permission.VIEW_SERVICES,
        }
    ),
}


def has_permission(
    assignments: Iterable[RoleAssignment],
    permission: Permission,
    deployment_id: uuid.UUID | None = None,
) -> bool:
    """Pure decision core, the unit under the table-driven contract tests.

    An org-wide assignment (deployment_id None) grants everywhere. A scoped
    assignment grants only when the request targets that same deployment.
    A scoped request (deployment_id given) is satisfied by either; an
    org-level request (None) only by an org-wide assignment.
    """
    for assignment in assignments:
        role = Role(assignment.role)
        if permission not in ROLE_PERMISSIONS[role]:
            continue
        if assignment.deployment_id is None:
            return True
        if deployment_id is not None and assignment.deployment_id == deployment_id:
            return True
    return False


def require_permission(
    permission: Permission, scope_path_param: str | None = None
) -> Callable[..., UserSession]:
    """Dependency factory: composes require_session, then authorizes.

    Applied at the API layer on every request (spec 12.3). When
    scope_path_param names a path parameter, the check is scoped to the
    deployment id in that parameter; otherwise it is an organization-level
    check. Usage:

        Depends(require_permission(Permission.MANAGE_CONFIG))
        Depends(require_permission(Permission.VIEW_STATUS, "deployment_id"))
    """

    def dependency(request: Request, session: SessionDep) -> UserSession:
        deployment_id: uuid.UUID | None = None
        if scope_path_param is not None:
            raw = request.path_params.get(scope_path_param)
            if raw is None:
                raise AppError(
                    "internal_error",
                    f"route is missing scope path parameter {scope_path_param!r}",
                    status_code=500,
                )
            try:
                deployment_id = uuid.UUID(str(raw))
            except ValueError as error:
                raise AppError(
                    "validation_error",
                    f"invalid deployment id {raw!r}",
                    status_code=422,
                ) from error
        if not has_permission(session.user.role_assignments, permission, deployment_id):
            raise AppError("forbidden", f"requires permission {permission.value}", status_code=403)
        return session

    return dependency
