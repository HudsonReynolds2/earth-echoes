"""The D52 config checksum, as firmware would implement it (task SIM.1).

**Reimplemented from the written recipe, never imported from the platform.**
`app.config.canonical` is right there on the path and this module deliberately
does not call it: a simulator standing in for firmware that calls the
platform's own function proves only that the function is self-consistent. Real
firmware has the recipe and not the code, so the harness has the recipe and not
the code — and a recipe that cannot be reimplemented from its description fails
in `tests/test_checksum_agreement.py` rather than in the field.

The recipe (DECISIONS D52, quoted clause by clause below):

    JSON with keys sorted at every depth, compact separators,
    `ensure_ascii=False`, UTF-8, no trailing newline; the checksum is
    `"sha256:"` plus the hex digest of those bytes.

Why it has to be exact: a device echoes the checksum of the config it applied
(spec 6.2, 7.3) and the platform matches by STRING EQUALITY. One space after a
separator, one key out of order, or one non-ASCII character escaped where the
platform left it alone, and every revision this fleet ever applies reads as
drift — for a reason no operator could find from the outside.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

#: The recipe's prefix. The platform's `Checksum` field type pins the whole
#: shape (`^sha256:[0-9a-f]{64}$`), so a device that invented its own prefix
#: would be rejected at the contract boundary rather than silently mismatch.
ALGORITHM = "sha256"


def _sorted_at_every_depth(value: Any) -> Any:
    """The recipe's "keys sorted at every depth", read literally.

    Rebuilding each mapping in sorted key order is a deliberately independent
    reading of that clause — `json.dumps(sort_keys=True)` is how the platform
    spells it, and taking the same shortcut here would make the cross-check
    test compare one implementation with itself. Lists are walked because a
    dict nested inside an array is still at a depth.

    `Any` rather than a recursive JSON alias: the values arrive from a decoded
    `DesiredConfig.config`, which the contract types as `JsonValue`, and a
    narrower annotation here would only be a second, weaker copy of that.
    """
    if isinstance(value, Mapping):
        return {key: _sorted_at_every_depth(value[key]) for key in sorted(value)}
    if isinstance(value, list | tuple):
        return [_sorted_at_every_depth(item) for item in value]
    return value


def canonical_bytes(snapshot: Mapping[str, Any]) -> bytes:
    """The exact bytes the checksum is taken over.

    Compact separators (no space after `,` or `:`), non-ASCII left as itself
    and encoded UTF-8, and nothing appended — `json.dumps` writes no trailing
    newline, and adding one would change every digest in the fleet.
    """
    return json.dumps(
        _sorted_at_every_depth(snapshot),
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def config_checksum(snapshot: Mapping[str, Any]) -> str:
    """`"sha256:"` plus the hex digest of `canonical_bytes(snapshot)`.

    Taken over the snapshot WITH secret markers in place, because that is what
    a desired payload carries: plaintext secrets never transit a desired topic
    (spec 5.4, 8), so the body the device received is the body the platform
    hashed, and the two checksums match by construction.
    """
    return f"{ALGORITHM}:{hashlib.sha256(canonical_bytes(snapshot)).hexdigest()}"
