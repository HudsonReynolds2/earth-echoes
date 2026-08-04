"""Sparse override storage service (task E2.2; DECISIONS D50-D51).

Stage-never-commit: the caller owns the transaction, like every inventory
service. Secrets are the exception, deliberately (D51): SecretStore commits
through its OWN sessions, so plaintext puts land immediately - an aborted
caller transaction leaves an unreachable secret whose marker never landed,
which is harmless - while deletions are RETURNED to the caller to run after
its commit, because deleting first would lose the value if the transaction
rolled back. Callers: run_after_commit(change.secret_names_to_delete).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.catalog import CATALOG_BY_KEY, CATALOG_VERSION, CatalogEntry
from app.config.validation import OverrideError, is_keep_sentinel, validate_override_map
from app.models import EntityOverride
from app.secrets import SecretStore


class OverrideValidationError(Exception):
    """Raised before anything is staged; the API layer folds errors into one
    D8 validation_error 422 with detail {"errors": [{key, code, message}]}."""

    def __init__(self, errors: list[OverrideError]) -> None:
        super().__init__(f"{len(errors)} invalid override key(s)")
        self.errors = errors


@dataclass(frozen=True)
class OverrideChange:
    """What a put changed, for the caller's audit row (key NAMES only, never
    values - the E0 audit convention)."""

    set_keys: tuple[str, ...]
    unset_keys: tuple[str, ...]
    secret_names_to_delete: tuple[str, ...]  # run through SecretStore AFTER commit


def secret_name(entity_type: str, entity_id: str, key: str) -> str:
    """The SecretStore name for a config secret (D51): the config: namespace
    beside totp:/deployment:/bundle: (INTERFACES, SecretStore section)."""
    return f"config:{entity_type}:{entity_id}:{key}"


def secret_marker(entity_type: str, entity_id: str, key: str) -> dict[str, str]:
    return {"$secret": secret_name(entity_type, entity_id, key)}


def is_secret_marker(value: Any) -> bool:
    return (
        isinstance(value, dict) and set(value) == {"$secret"} and isinstance(value["$secret"], str)
    )


def get_override_row(db: Session, entity_type: str, entity_id: str) -> EntityOverride | None:
    return db.scalar(
        select(EntityOverride).where(
            EntityOverride.entity_type == entity_type,
            EntityOverride.entity_id == entity_id,
        )
    )


def get_overrides(db: Session, entity_type: str, entity_id: str) -> dict[str, Any]:
    """The stored sparse map, markers included (raw). Redaction is the merge
    engine's job (E2.3); never hand this to a response without it."""
    row = get_override_row(db, entity_type, entity_id)
    return dict(row.overrides) if row is not None else {}


def put_overrides(
    db: Session,
    secret_store: SecretStore,
    entity_type: str,
    entity_id: str,
    new_map: Mapping[str, Any],
    *,
    catalog: Mapping[str, CatalogEntry] | None = None,
    catalog_version: int = CATALOG_VERSION,
) -> OverrideChange:
    """Wholesale replace of one entity's sparse map (the E1.7 tags precedent:
    PUT is never a merge). Secret keys: a plaintext string stores the value
    in SecretStore and the marker here; the keep sentinel preserves the
    stored secret; omission unsets it (deletion deferred to post-commit).
    Raises OverrideValidationError before staging anything."""
    entries = catalog if catalog is not None else CATALOG_BY_KEY
    errors = validate_override_map(new_map, entries, entity_level=entity_type)
    row = get_override_row(db, entity_type, entity_id)
    old = dict(row.overrides) if row is not None else {}
    for key, value in new_map.items():
        entry = entries.get(key)
        keeps = entry is not None and entry.secret and is_keep_sentinel(value)
        if keeps and not is_secret_marker(old.get(key)):
            errors.append(
                OverrideError(
                    key,
                    "invalid_value",
                    f"{key} has no stored secret to keep; provide a replacement value",
                )
            )
    if errors:
        raise OverrideValidationError(sorted(errors, key=lambda e: e.key))

    stored: dict[str, Any] = {}
    set_keys: list[str] = []
    for key, value in new_map.items():
        entry = entries[key]
        if entry.secret:
            if is_keep_sentinel(value):
                stored[key] = old[key]
            else:
                secret_store.put(secret_name(entity_type, entity_id, key), value)
                stored[key] = secret_marker(entity_type, entity_id, key)
                set_keys.append(key)  # replacement counts even though the marker is stable
        else:
            stored[key] = value
            if key not in old or old[key] != value:
                set_keys.append(key)

    unset_keys = sorted(set(old) - set(new_map))
    to_delete = tuple(old[key]["$secret"] for key in unset_keys if is_secret_marker(old.get(key)))

    if not stored:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(
            EntityOverride(
                entity_type=entity_type,
                entity_id=entity_id,
                overrides=stored,
                catalog_version=catalog_version,
            )
        )
    else:
        row.overrides = stored
        row.catalog_version = catalog_version
    return OverrideChange(
        set_keys=tuple(sorted(set_keys)),
        unset_keys=tuple(unset_keys),
        secret_names_to_delete=to_delete,
    )


def delete_overrides_for(db: Session, entity_type: str, entity_id: str) -> tuple[str, ...]:
    """Entity-deletion cleanup (E2.4 wires this into the E1 DELETE
    endpoints). Removes the row, returns the config secret names for the
    caller's post-commit SecretStore deletion."""
    row = get_override_row(db, entity_type, entity_id)
    if row is None:
        return ()
    names = tuple(value["$secret"] for value in row.overrides.values() if is_secret_marker(value))
    db.delete(row)
    return names
