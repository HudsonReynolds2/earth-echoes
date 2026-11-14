"""The map's read surface (epic E6; spec 9, 13).

E6.1 ships `GET /map/config`. E6.4 adds the per-Deployment map reads here.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.map.basemaps import MapConfigOut, map_config
from app.models import UserSession
from app.scoping import require_any_assignment

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
