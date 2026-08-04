"""Effective-config merge engine (task E2.3; spec 5.1; DECISIONS D52-D53).

TEST-CRITICAL (spec 14.5): tests/test_config_merge.py is the documentation
of these semantics and may never be weakened (rule R0). Pure - this module
imports nothing that touches a database; app/config/service.py walks the
hierarchy and feeds it.

The semantics, in one paragraph (D53): spec 5.1's "deep merge" IS the level
cascade itself. The catalog's keys are flat dotted strings; for each key the
winner is the DEEPEST chain level that sets it, else the catalog default,
and the winning value replaces wholesale - capture.schedule objects
included, the E1.7 replace-never-merge precedent. Inventory-resolved keys
never merge: they materialize from listener columns at listener level and
are omitted everywhere else. Secret markers ride the cascade like any value;
redaction and resolution are explicit second passes.
"""

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.config.catalog import LEVELS, CatalogEntry
from app.config.validation import KEEP_SENTINEL, is_secret_marker

_DEPTH = {level: depth for depth, level in enumerate(LEVELS)}


@dataclass(frozen=True)
class LevelOverrides:
    """One chain link: an entity's sparse override map. Chains are ordered
    root -> target; levels with no override row are simply absent."""

    level: str  # one of LEVELS
    entity_id: str  # UUID string, or MAC for listeners
    overrides: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedValue:
    """One key's resolution with provenance (the E2.4 wire shape)."""

    value: Any
    source: str  # LEVELS | "default" | "inventory"
    source_entity_id: str | None  # None for "default"


def effective_config(
    chain: Sequence[LevelOverrides],
    catalog: Mapping[str, CatalogEntry],
    *,
    target_level: str,
    inventory: Mapping[str, Any] | None = None,
    inventory_entity_id: str | None = None,
) -> dict[str, ResolvedValue]:
    """Merge a chain into the target's effective config, RAW: secret markers
    ride verbatim (redact_secrets before any response; resolve_secret_refs
    only on the internal E3/E4 paths).

    Covers every catalog key at every level - an ancestor's effective value
    for a below-lowest-level key is exactly what its descendants inherit -
    EXCEPT inventory-resolved keys, which appear only at listener level
    (from `inventory`, provenance "inventory") and are omitted elsewhere.
    Chain overrides of inventory keys and unknown keys are ignored, not
    errors: storage validates writes (E2.2); the merge is defensive on read.
    """
    if target_level not in _DEPTH:
        raise ValueError(f"unknown target level {target_level!r}")
    _check_chain(chain, target_level)
    resolved: dict[str, ResolvedValue] = {}
    for key, entry in catalog.items():
        if entry.resolution == "inventory":
            if target_level == "listener":
                value = None if inventory is None else inventory.get(key)
                resolved[key] = ResolvedValue(
                    value=value, source="inventory", source_entity_id=inventory_entity_id
                )
            continue
        winner: ResolvedValue | None = None
        for link in chain:  # root -> target; the last setter is the deepest
            if key in link.overrides:
                winner = ResolvedValue(
                    value=_own(link.overrides[key]),
                    source=link.level,
                    source_entity_id=link.entity_id,
                )
        if winner is None:
            winner = ResolvedValue(
                value=_own(entry.default), source="default", source_entity_id=None
            )
        resolved[key] = winner
    return resolved


def _own(value: Any) -> Any:
    """Container values are copied on the way out so mutating a result never
    reaches back into the chain or the catalog (side-effect freedom is a
    documented suite case); scalars pass through."""
    return copy.deepcopy(value) if isinstance(value, dict | list) else value


def redact_secrets(
    config: Mapping[str, ResolvedValue], catalog: Mapping[str, CatalogEntry]
) -> dict[str, ResolvedValue]:
    """The response-safe view (D51): any SET secret value - marker or
    otherwise - renders as the keep sentinel; an unset secret stays None.
    Safe for API responses, the UI, and audit surfaces."""
    redacted: dict[str, ResolvedValue] = {}
    for key, rv in config.items():
        entry = catalog.get(key)
        if entry is not None and entry.secret and rv.value is not None:
            redacted[key] = ResolvedValue(
                value=dict(KEEP_SENTINEL), source=rv.source, source_entity_id=rv.source_entity_id
            )
        else:
            redacted[key] = rv
    return redacted


def resolve_secret_refs(
    config: Mapping[str, ResolvedValue],
    catalog: Mapping[str, CatalogEntry],
    get: Callable[[str], str],
) -> dict[str, ResolvedValue]:
    """INTERNAL ONLY - for E3's publisher and E4's bundle generator, never
    reachable over HTTP (INTERFACES, merge engine section). Substitutes each
    secret marker with the plaintext from `get(name)` (SecretStore.get on
    the internal paths; injected here so the core stays pure)."""
    resolved: dict[str, ResolvedValue] = {}
    for key, rv in config.items():
        if is_secret_marker(rv.value):
            resolved[key] = ResolvedValue(
                value=get(rv.value["$secret"]),
                source=rv.source,
                source_entity_id=rv.source_entity_id,
            )
        else:
            resolved[key] = rv
    return resolved


def _check_chain(chain: Sequence[LevelOverrides], target_level: str) -> None:
    """Malformed chains are programming errors in the caller, never data:
    fail loud, don't guess."""
    last = -1
    for link in chain:
        depth = _DEPTH.get(link.level)
        if depth is None:
            raise ValueError(f"unknown chain level {link.level!r}")
        if depth <= last:
            raise ValueError("chain must be ordered root -> target without duplicate levels")
        if depth > _DEPTH[target_level]:
            raise ValueError(f"chain level {link.level!r} is below the target {target_level!r}")
        last = depth
