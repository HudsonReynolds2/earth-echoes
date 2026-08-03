"""Bulk import engine (task E1.6; spec 13; DECISIONS D38).

Row-by-row under one transaction with a SAVEPOINT per row: constraint
violations surface as row errors without aborting the surrounding
transaction, and flushed rows are visible to later rows' collision checks -
so in-file duplicates and DB duplicates fall out of the same code path.

Commit semantics (D38): all-or-nothing by default - any failed row rolls
EVERYTHING back, including the audit row, and the report answers
committed=false with per-row results (this doubles as the dry run the UI
shows before a partial accept). `partial=true` commits the valid rows only.
Either way the request is a 200 with a report: row results are data, not an
error envelope, and row `error.code` strings reuse the D8 vocabulary as data
without extending the wire codes.
"""

import uuid
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth.rbac import Permission, has_permission
from app.errors import AppError
from app.inventory.naming import next_free_name, normalize_mac
from app.models import Aggregator, Listener, Pod, RoleAssignment

MAX_ROWS = 1000
MAX_BYTES = 1024 * 1024

LISTENER_CSV_COLUMNS = ["mac", "name", "aggregator_uuid", "gps_lat", "gps_lon", "tags"]
AGGREGATOR_CSV_COLUMNS = ["pod_id", "aggregator_uuid", "balena_uuid", "name", "tags"]


class ListenerImportRow(BaseModel):
    model_config = {"extra": "forbid"}

    mac: str
    name: str = Field(min_length=1, max_length=200)
    aggregator_uuid: str = Field(min_length=1, max_length=64)
    gps_lat: float | None = Field(default=None, ge=-90, le=90)
    gps_lon: float | None = Field(default=None, ge=-180, le=180)
    tags: list[str] = Field(default_factory=list)


class AggregatorImportRow(BaseModel):
    model_config = {"extra": "forbid"}

    pod_id: uuid.UUID
    aggregator_uuid: str | None = Field(default=None, min_length=1, max_length=64)
    balena_uuid: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list)


class RowError(BaseModel):
    code: str  # D8 strings as row data: validation_error | conflict | forbidden
    message: str


class RowResult(BaseModel):
    row: int  # 1-based data row number (CSV: first row after the header)
    status: str  # "created" | "error"
    entity_id: str | None = None
    name: str | None = None
    error: RowError | None = None


class ImportReport(BaseModel):
    committed: bool
    created: int
    failed: int
    rows: list[RowResult]


def parse_csv_rows(text: str, columns: list[str]) -> list[dict[str, Any]]:
    """Strict-header CSV to row dicts. Empty cells become None; `tags` is
    pipe-separated. A wrong header is a whole-request 422 - the file is
    malformed, not a row problem."""
    import csv
    import io

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or [name.strip() for name in reader.fieldnames] != columns:
        raise AppError(
            "validation_error",
            f"CSV header must be exactly: {','.join(columns)}",
            status_code=422,
        )
    rows: list[dict[str, Any]] = []
    for record in reader:
        cleaned: dict[str, Any] = {}
        for key, value in record.items():
            if key is None:
                raise AppError(
                    "validation_error", "CSV row has more cells than the header", status_code=422
                )
            stripped = value.strip() if isinstance(value, str) else value
            if key == "tags":
                cleaned[key] = [t.strip() for t in (stripped or "").split("|") if t.strip()]
            else:
                cleaned[key] = stripped if stripped else None
        # Optional-column Nones are fine; required columns fail per-row below.
        cleaned = {k: v for k, v in cleaned.items() if v is not None or k in ("tags",)}
        rows.append(cleaned)
    return rows


def _clean_tags(tags: Iterable[str]) -> list[str]:
    return sorted({tag.strip() for tag in tags if tag.strip()})


def _error(index: int, code: str, message: str) -> RowResult:
    return RowResult(row=index, status="error", error=RowError(code=code, message=message))


def _finish(
    db: Session,
    *,
    action: str,
    entity_type: str,
    actor_user_id: uuid.UUID | None,
    results: list[RowResult],
    partial: bool,
    auto_suffix: bool,
) -> ImportReport:
    """Shared commit/rollback tail: audit rides the same transaction, so an
    all-or-nothing failure provably leaves no audit row either."""
    failed = sum(1 for r in results if r.status == "error")
    valid = [r for r in results if r.status == "created"]
    if failed and not partial:
        db.rollback()
        return ImportReport(committed=False, created=0, failed=failed, rows=results)
    record_audit(
        db,
        action=action,
        entity_type=entity_type,
        entity_id=f"import:{uuid.uuid4().hex[:12]}",
        actor_user_id=actor_user_id,
        detail={
            "created": len(valid),
            "failed": failed,
            "partial": partial,
            "auto_suffix": auto_suffix,
            "created_ids": [r.entity_id for r in valid],
        },
    )
    db.commit()
    return ImportReport(committed=True, created=len(valid), failed=failed, rows=results)


def import_listeners(
    db: Session,
    assignments: list[RoleAssignment],
    actor_user_id: uuid.UUID | None,
    rows_data: list[dict[str, Any]],
    *,
    partial: bool,
    auto_suffix: bool,
) -> ImportReport:
    if len(rows_data) > MAX_ROWS:
        raise AppError("validation_error", f"import exceeds {MAX_ROWS} rows", status_code=422)
    results: list[RowResult] = []
    for index, data in enumerate(rows_data, start=1):
        try:
            parsed = ListenerImportRow.model_validate(data)
        except ValidationError as error:
            first = error.errors()[0]
            results.append(_error(index, "validation_error", f"{first['loc']}: {first['msg']}"))
            continue
        try:
            mac = normalize_mac(parsed.mac)
        except AppError as error:
            results.append(_error(index, "validation_error", error.message))
            continue
        aggregator = db.scalars(
            select(Aggregator).where(Aggregator.aggregator_uuid == parsed.aggregator_uuid)
        ).first()
        if aggregator is None:
            results.append(
                _error(
                    index, "validation_error", f"unknown aggregator_uuid {parsed.aggregator_uuid!r}"
                )
            )
            continue
        deployment_id = db.scalar(select(Pod.deployment_id).where(Pod.id == aggregator.pod_id))
        if deployment_id is None or not has_permission(
            assignments, Permission.MANAGE_DEVICES, deployment_id
        ):
            results.append(
                _error(index, "forbidden", "no manage_devices grant in this row's deployment")
            )
            continue
        if db.get(Listener, mac) is not None:  # sees rows flushed earlier in THIS file too
            results.append(_error(index, "conflict", f"MAC {mac} is already registered"))
            continue
        final_name = parsed.name
        collides = (
            db.scalar(
                select(Listener.mac).where(
                    Listener.deployment_id == deployment_id, Listener.name == final_name
                )
            )
            is not None
        )
        if collides:
            if not auto_suffix:
                results.append(
                    _error(
                        index,
                        "conflict",
                        f"listener name {parsed.name!r} already exists in this deployment",
                    )
                )
                continue
            final_name = next_free_name(db, deployment_id, parsed.name)
        nested = db.begin_nested()
        try:
            db.add(
                Listener(
                    mac=mac,
                    name=final_name,
                    aggregator_id=aggregator.id,
                    deployment_id=deployment_id,
                    gps_lat=parsed.gps_lat,
                    gps_lon=parsed.gps_lon,
                    tags=_clean_tags(parsed.tags),
                )
            )
            db.flush()
            nested.commit()
            results.append(RowResult(row=index, status="created", entity_id=mac, name=final_name))
        except IntegrityError:
            nested.rollback()
            results.append(_error(index, "conflict", "MAC or name collided during the import"))
    return _finish(
        db,
        action="listener.import",
        entity_type="listener",
        actor_user_id=actor_user_id,
        results=results,
        partial=partial,
        auto_suffix=auto_suffix,
    )


def import_aggregators(
    db: Session,
    assignments: list[RoleAssignment],
    actor_user_id: uuid.UUID | None,
    rows_data: list[dict[str, Any]],
    *,
    partial: bool,
) -> ImportReport:
    if len(rows_data) > MAX_ROWS:
        raise AppError("validation_error", f"import exceeds {MAX_ROWS} rows", status_code=422)
    results: list[RowResult] = []
    for index, data in enumerate(rows_data, start=1):
        try:
            parsed = AggregatorImportRow.model_validate(data)
        except ValidationError as error:
            first = error.errors()[0]
            results.append(_error(index, "validation_error", f"{first['loc']}: {first['msg']}"))
            continue
        pod = db.get(Pod, parsed.pod_id)
        if pod is None:
            results.append(_error(index, "validation_error", f"unknown pod_id {parsed.pod_id}"))
            continue
        if not has_permission(assignments, Permission.MANAGE_DEVICES, pod.deployment_id):
            results.append(
                _error(index, "forbidden", "no manage_devices grant in this row's deployment")
            )
            continue
        aggregator_uuid = parsed.aggregator_uuid or uuid.uuid4().hex
        nested = db.begin_nested()
        try:
            row = Aggregator(
                pod_id=parsed.pod_id,
                aggregator_uuid=aggregator_uuid,
                balena_uuid=parsed.balena_uuid,
                name=parsed.name,
                tags=_clean_tags(parsed.tags),
            )
            db.add(row)
            db.flush()
            nested.commit()
            results.append(
                RowResult(row=index, status="created", entity_id=str(row.id), name=aggregator_uuid)
            )
        except IntegrityError:
            nested.rollback()
            results.append(
                _error(
                    index,
                    "conflict",
                    "pod already has its aggregator, or aggregator_uuid already exists",
                )
            )
    return _finish(
        db,
        action="aggregator.import",
        entity_type="aggregator",
        actor_user_id=actor_user_id,
        results=results,
        partial=partial,
        auto_suffix=False,
    )
