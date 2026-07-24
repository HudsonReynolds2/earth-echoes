"""List-endpoint contract (task E0.3; decision D7). Binding on E1 through E7.

Request: limit (1..500, default 50), offset (>= 0), sort. Sort grammar:
`sort=name` ascending, `sort=-created_at` descending, comma-separated for
multi-key. Response: {"items": [...], "total": int, "limit": int, "offset": int}.

E0.9's /users list is the first real consumer; until then the throwaway model
in the test suite exercises it.
"""

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select, UnaryExpression
from sqlalchemy.orm import InstrumentedAttribute

from app.errors import AppError

SortableColumns = dict[str, "InstrumentedAttribute[object]"]


class PageParams(BaseModel):
    limit: Annotated[int, Query(ge=1, le=500)] = 50
    offset: Annotated[int, Query(ge=0)] = 0
    sort: str | None = None


class ListResponse[ItemT](BaseModel):
    items: list[ItemT]
    total: int
    limit: int
    offset: int


def parse_sort(sort: str | None, allowed: SortableColumns) -> list[UnaryExpression[object]]:
    """Translate the D7 sort grammar into ORDER BY clauses.

    Unknown fields raise the envelope's validation_error rather than being
    ignored, so a typo never silently changes result order.
    """
    if not sort:
        return []
    clauses: list[UnaryExpression[object]] = []
    for raw in sort.split(","):
        token = raw.strip()
        if not token:
            continue
        descending = token.startswith("-")
        name = token[1:] if descending else token
        column = allowed.get(name)
        if column is None:
            raise AppError(
                "validation_error",
                f"unknown sort field {name!r}",
                status_code=422,
                detail={"allowed": sorted(allowed)},
            )
        clauses.append(column.desc() if descending else column.asc())
    return clauses


def apply_page(
    statement: Select[tuple[object, ...]],
    params: PageParams,
    allowed: SortableColumns,
) -> Select[tuple[object, ...]]:
    """Apply D7 sorting and windowing to a Select."""
    clauses = parse_sort(params.sort, allowed)
    if clauses:
        statement = statement.order_by(*clauses)
    return statement.limit(params.limit).offset(params.offset)
