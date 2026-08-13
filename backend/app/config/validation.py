"""Override validation against the catalog (task E2.2; DECISIONS D50-D51).

Pure - no database imports. Every error names its offending key (the E2.1
acceptance). The level rule is spec 5.3's, owner-resolved over the phase
doc's inverted wording (D50; project-changes #17, addendum PHASE2-4-01): a
key may be overridden AT its lowest level or at any ANCESTOR level, never
below it - network.wifi_ssid (Pod) is settable at organization, deployment,
or pod, and rejected at aggregator or listener.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.config.catalog import LEVELS, CatalogEntry

# Wire sentinel for "keep the stored secret" (D51): a redacted GET renders a
# set secret as this exact object, so a client can round-trip the response
# back through PUT without ever holding the plaintext.
KEEP_SENTINEL: dict[str, Any] = {"$secret_set": True}

LEVEL_DEPTH: dict[str, int] = {level: depth for depth, level in enumerate(LEVELS)}

# capture.schedule is stored and delivered opaquely (its schema is firmware
# territory, E4); the cap keeps opaque from meaning unbounded.
MAX_OBJECT_BYTES = 2048


@dataclass(frozen=True)
class OverrideError:
    """One rejected key. code is detail vocabulary inside the D8
    validation_error envelope, not a new wire code."""

    key: str
    code: str  # unknown_key | inventory_resolved | service_restricted | level_rule | invalid_value
    message: str


def effective_lowest_level(entry: CatalogEntry) -> str:
    """'any' (logging.verbosity) behaves as listener for the level rule
    (D47): meaningful everywhere means settable everywhere."""
    return "listener" if entry.lowest_level == "any" else entry.lowest_level


def is_keep_sentinel(value: Any) -> bool:
    return bool(value == KEEP_SENTINEL)


def is_secret_marker(value: Any) -> bool:
    """A stored secret reference (D51): {"$secret": "config:..."}. Lives here
    rather than overrides.py so the pure merge engine (E2.3) can recognize
    markers without importing anything database-touching."""
    return (
        isinstance(value, dict) and set(value) == {"$secret"} and isinstance(value["$secret"], str)
    )


def validate_override_map(
    overrides: Mapping[str, Any],
    catalog: Mapping[str, CatalogEntry],
    entity_level: str,
    *,
    allow_write_restricted: bool = False,
) -> list[OverrideError]:
    """Validate a full sparse override map for one entity level. Returns
    every error (sorted by key), never just the first - the API folds them
    into one 422 so a bulk edit fails atomically and informatively.

    `allow_write_restricted` is the ONE door through which the twelve
    service-onboarding keys can be written (E5.7a, phase-5 fixed choice 3),
    and it is keyword-only and defaulted off so that every existing caller -
    which is every operator-facing path - keeps refusing them. **The
    `service_restricted` 422 has its own independent test precisely so this
    shortcut cannot pass unnoticed**; deleting the check instead of gating it
    is what this parameter exists to avoid. Nothing else about validation
    changes: the level rule, the value rules and the inventory-resolved
    refusal all still apply to these keys.
    """
    if entity_level not in LEVEL_DEPTH:
        raise ValueError(f"unknown entity level {entity_level!r}")
    errors: list[OverrideError] = []
    for key in sorted(overrides):
        error = _validate_key(
            key,
            overrides[key],
            catalog,
            entity_level,
            allow_write_restricted=allow_write_restricted,
        )
        if error is not None:
            errors.append(error)
    return errors


def _validate_key(
    key: str,
    value: Any,
    catalog: Mapping[str, CatalogEntry],
    entity_level: str,
    *,
    allow_write_restricted: bool = False,
) -> OverrideError | None:
    entry = catalog.get(key)
    if entry is None:
        return OverrideError(key, "unknown_key", f"unknown setting key {key!r}")
    if entry.resolution == "inventory":
        return OverrideError(
            key,
            "inventory_resolved",
            f"{key} is inventory-resolved; edit it via PATCH /listeners/{{mac}}, "
            "not config overrides",
        )
    if entry.write_restricted is not None and not allow_write_restricted:
        return OverrideError(
            key,
            "service_restricted",
            f"{key} is written by the deployment services onboarding flow (E5, "
            "spec 16); it cannot be set through config overrides",
        )
    lowest = effective_lowest_level(entry)
    if LEVEL_DEPTH[entity_level] > LEVEL_DEPTH[lowest]:
        return OverrideError(
            key,
            "level_rule",
            f"{key} resolves at {lowest} level; it may be overridden there or at "
            f"an ancestor level, not below (spec 5.3) - rejected at {entity_level}",
        )
    message = _validate_value(entry, value)
    if message is not None:
        return OverrideError(key, "invalid_value", f"{key}: {message}")
    return None


def _validate_value(entry: CatalogEntry, value: Any) -> str | None:
    if value is None:
        return "null is not a value; remove the key to unset it"
    if entry.secret:
        if is_keep_sentinel(value):
            return None
        if isinstance(value, str):
            return None if value else "secret replacement must be a non-empty string"
        return 'secret keys take a replacement string or the keep sentinel {"$secret_set": true}'
    if entry.value_type == "bool":
        return None if isinstance(value, bool) else "expected a boolean"
    if entry.value_type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return "expected an integer"
        return _check_bounds(entry, value)
    if entry.value_type == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return "expected a number"
        return _check_bounds(entry, value)
    if entry.value_type == "string":
        if not isinstance(value, str):
            return "expected a string"
        if entry.enum_values is not None and value not in entry.enum_values:
            return f"must be one of {list(entry.enum_values)}"
        return None
    if entry.value_type == "object":
        if not isinstance(value, dict):
            return "expected an object"
        size = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
        if size > MAX_OBJECT_BYTES:
            return f"object exceeds {MAX_OBJECT_BYTES} bytes ({size})"
        return None
    raise ValueError(f"catalog entry {entry.key!r} has unknown value_type {entry.value_type!r}")


def _check_bounds(entry: CatalogEntry, value: int | float) -> str | None:
    if entry.enum_values is not None and value not in entry.enum_values:
        return f"must be one of {list(entry.enum_values)}"
    if entry.min_value is not None and value < entry.min_value:
        return f"must be >= {entry.min_value}"
    if entry.max_value is not None and value > entry.max_value:
        return f"must be <= {entry.max_value}"
    return None
