"""Deployment-scoped visibility (task E1.2; DECISIONS D35).

require_permission (E0.7) gates a request; nothing in E0 filters result sets.
These helpers are that filter, single-sourced here so E2-E7 reuse them instead
of growing per-epic variants:

- visible_deployments(assignments, permission) -> "all" | set[UUID]
- scope_filter(statement, column, scope) applies the visibility to a Select
- require_any_assignment gates surfaces every role may read (the organization
  row, hierarchy lists) without demanding an org-wide grant the way an
  org-level require_permission check would.

The 403/404 asymmetry (D35): /deployments/{deployment_id} routes keep E0.7's
403-before-lookup pattern - safe because the check runs before any existence
lookup. Child items (pod/aggregator/listener) must be fetched first, so an
out-of-scope hit answers 404, never 403: MACs are enumerable (OUI + counter),
and 403-on-existing would be an existence oracle.
"""

import uuid
from collections.abc import Iterable
from typing import Any, Literal

from sqlalchemy import Select, false
from sqlalchemy.orm import InstrumentedAttribute

from app.auth.deps import SessionDep
from app.auth.rbac import ROLE_PERMISSIONS, Permission, Role
from app.errors import AppError
from app.models import RoleAssignment, UserSession

DeploymentScope = Literal["all"] | set[uuid.UUID]


def visible_deployments(
    assignments: Iterable[RoleAssignment], permission: Permission
) -> DeploymentScope:
    """The deployments a user may exercise `permission` in.

    An org-wide assignment whose role carries the permission short-circuits to
    "all"; otherwise the set of scoped deployment ids whose role carries it
    (possibly empty - the caller renders an empty list, not an error).
    """
    scoped: set[uuid.UUID] = set()
    for assignment in assignments:
        role = Role(assignment.role)
        if permission not in ROLE_PERMISSIONS[role]:
            continue
        if assignment.deployment_id is None:
            return "all"
        scoped.add(assignment.deployment_id)
    return scoped


def scope_filter[RowT](
    statement: Select[tuple[RowT]],
    deployment_id_column: "InstrumentedAttribute[Any]",
    scope: DeploymentScope,
) -> Select[tuple[RowT]]:
    """Apply a visibility scope to a list statement.

    "all" is a no-op; an empty set compiles to WHERE false (an empty page,
    deliberately not an error - a viewer with no grants sees empty lists).
    """
    if scope == "all":
        return statement
    if not scope:
        return statement.where(false())
    return statement.where(deployment_id_column.in_(scope))


def require_any_assignment(session: SessionDep) -> UserSession:
    """Dependency for surfaces every role may read (hierarchy lists, the
    organization row): a session plus at least one role assignment. Scoped
    visibility is then the endpoint's job via scope_filter."""
    if not session.user.role_assignments:
        raise AppError("forbidden", "requires at least one role assignment", status_code=403)
    return session
