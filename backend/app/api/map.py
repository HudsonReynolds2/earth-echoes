"""The map's read surface (epic E6; spec 9, 13).

E6.1 ships `GET /map/config`; E6.4 adds the two drill-level reads. All of
them are open to every role (all four hold `VIEW_STATUS`, E0.7), scoped to the
Deployments the caller may see.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.hierarchy_common import not_found
from app.auth.deps import DbDep
from app.auth.rbac import Permission, has_permission
from app.map.basemaps import MapConfigOut, map_config
from app.map.service import (
    MapDeploymentDetailOut,
    MapDeploymentOut,
    deployment_detail,
    deployments_layer,
)
from app.models import Deployment, UserSession
from app.scoping import require_any_assignment, visible_deployments

router = APIRouter(prefix="/map", tags=["map"])


@router.get("/config", response_model=MapConfigOut)
def get_map_config(
    request: Request,
    _: Annotated[UserSession, Depends(require_any_assignment)],
) -> MapConfigOut:
    """The basemaps this deployment of the platform offers (D188).

    Gated on any role assignment rather than on an org-level `VIEW_STATUS`.
    Every role holds `VIEW_STATUS` (E0.7), and an operator scoped to one
    Deployment still needs the basemap to see that Deployment. Nothing here is
    scoped data: it is the platform's own configuration.
    """
    return map_config(request.app.state.settings)


@router.get("/deployments", response_model=list[MapDeploymentOut])
def get_map_deployments(
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> list[MapDeploymentOut]:
    """The Organization layer: one rolled-up row per visible Deployment
    (spec 9.2, 9.3). Not paginated: v1's order of ten Deployments (spec 14.2)
    is one marker each, and a map that showed only the first page would hide
    the rest of the organization."""
    scope = visible_deployments(session.user.role_assignments, Permission.VIEW_STATUS)
    return deployments_layer(db, scope)


@router.get("/deployments/{deployment_id}", response_model=MapDeploymentDetailOut)
def get_map_deployment(
    deployment_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> MapDeploymentDetailOut:
    """One Deployment's Pods and Listeners, rolled up (spec 9.2).

    A Deployment outside the caller's scope is a 404, not a 403, as the
    hierarchy endpoints do (D35): a 403 would confirm it exists.
    """
    deployment = db.get(Deployment, deployment_id)
    if deployment is None or not has_permission(
        session.user.role_assignments, Permission.VIEW_STATUS, deployment_id
    ):
        raise not_found("deployment")
    return deployment_detail(db, deployment)
