"""Bulk import endpoints (task E1.6; spec 13; DECISIONS D38).

One endpoint per entity, two content types: `application/json` bodies carry
{"rows": [...]}; `text/csv` bodies are the raw file (column format is
normative in docs/INTERFACES.md; guide/bulk-import.md shows operator
examples). Options ride the query string because a CSV body cannot carry
them: ?partial=true (default false = all-or-nothing) and ?auto_suffix=true
(listeners only, default false, never silent - E1.4's rule).

A well-formed request always answers 200 with the job report; row results
are data, not an error envelope. Scope is enforced per row (a cross-scope
row is a row-level `forbidden`), so the endpoints themselves need only a
session + CSRF.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request

from app.auth.deps import DbDep, require_csrf
from app.errors import AppError
from app.inventory.importer import (
    AGGREGATOR_CSV_COLUMNS,
    LISTENER_CSV_COLUMNS,
    MAX_BYTES,
    ImportReport,
    import_aggregators,
    import_listeners,
    parse_csv_rows,
)
from app.models import UserSession

router = APIRouter()


async def _raw_body(request: Request) -> bytes:
    """Raw request body, bypassing FastAPI's content-type-driven parsing: a
    JSON content type would otherwise be parsed and then fail validation
    against a bytes parameter. The import endpoints do their own dispatch on
    the content type (JSON rows vs raw CSV)."""
    return await request.body()


RawBody = Annotated[bytes, Depends(_raw_body)]


def _rows_from_body(
    raw: bytes, content_type: str | None, columns: list[str]
) -> list[dict[str, Any]]:
    if len(raw) > MAX_BYTES:
        raise AppError(
            "validation_error", f"import body exceeds {MAX_BYTES} bytes", status_code=422
        )
    media = (content_type or "").split(";")[0].strip().lower()
    if media == "text/csv":
        return parse_csv_rows(raw.decode("utf-8-sig"), columns)
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise AppError(
            "validation_error",
            "body must be application/json {rows: [...]} or text/csv",
            status_code=422,
        ) from error
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise AppError("validation_error", 'JSON imports carry {"rows": [...]}', status_code=422)
    return rows


@router.post("/listeners/import", response_model=ImportReport)
def import_listeners_endpoint(
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
    raw: RawBody,
    content_type: Annotated[str | None, Header(alias="content-type")] = None,
    partial: bool = False,
    auto_suffix: bool = False,
) -> ImportReport:
    rows = _rows_from_body(raw, content_type, LISTENER_CSV_COLUMNS)
    return import_listeners(
        db,
        actor.user.role_assignments,
        actor.user_id,
        rows,
        partial=partial,
        auto_suffix=auto_suffix,
    )


@router.post("/aggregators/import", response_model=ImportReport)
def import_aggregators_endpoint(
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
    raw: RawBody,
    content_type: Annotated[str | None, Header(alias="content-type")] = None,
    partial: bool = False,
) -> ImportReport:
    rows = _rows_from_body(raw, content_type, AGGREGATOR_CSV_COLUMNS)
    return import_aggregators(db, actor.user.role_assignments, actor.user_id, rows, partial=partial)
