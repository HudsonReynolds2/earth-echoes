"""Canonical JSON and the config checksum (task E2.3; DECISIONS D52).

THE byte-stability contract E3's reconciliation depends on: a device echoes
the checksum of the desired payload it applied (spec 6.2, 7.3), and matching
is string equality on these values - so the recipe below is FROZEN and
golden-pinned by the locked merge suite. Change nothing here without
treating it as a wire-protocol break.

Recipe: JSON with keys sorted at every depth, compact separators, non-ASCII
preserved (ensure_ascii=False), UTF-8 encoded, no trailing newline; checksum
is "sha256:" + the hex digest of those bytes. Checksums cover snapshots WITH
secret markers in place - secrets never transit desired topics (spec 5.4,
8), so the snapshot is the publishable payload body and device-echoed
checksums match by construction.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_config_bytes(snapshot: Mapping[str, Any]) -> bytes:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def config_checksum(snapshot: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_config_bytes(snapshot)).hexdigest()
