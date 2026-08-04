"""Settings catalog surface (task E2.1; spec 5.3, 13; DECISIONS D47).

The catalog read is a schema document, not a D7 list (D47): the frontend
renders its editors from it wholesale, so it ships unpaginated with a
top-level version and items sorted by key for deterministic rendering.
E2.6 adds POST /config/preview and POST /config/apply beside it.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.deps import DbDep
from app.models import SettingsCatalog
from app.scoping import require_any_assignment

router = APIRouter(prefix="/config")


class CatalogItemOut(BaseModel):
    key: str
    value_type: str
    enum_values: list[Any] | None
    min_value: float | None
    max_value: float | None
    default: Any
    lowest_level: str
    secret: bool
    resolution: str
    write_restricted: str | None
    notes: str


class CatalogOut(BaseModel):
    version: int
    items: list[CatalogItemOut]


@router.get("/catalog", response_model=CatalogOut, dependencies=[Depends(require_any_assignment)])
def get_catalog(db: DbDep) -> CatalogOut:
    rows = db.scalars(select(SettingsCatalog).order_by(SettingsCatalog.key)).all()
    return CatalogOut(
        version=max((row.version for row in rows), default=0),
        items=[
            CatalogItemOut(
                key=row.key,
                value_type=row.value_type,
                enum_values=row.enum_values,
                min_value=row.min_value,
                max_value=row.max_value,
                default=row.default_value,
                lowest_level=row.lowest_level,
                secret=row.secret,
                resolution=row.resolution,
                write_restricted=row.write_restricted,
                notes=row.notes,
            )
            for row in rows
        ],
    )
