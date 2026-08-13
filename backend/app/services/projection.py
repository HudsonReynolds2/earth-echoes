"""The five service rows, projected onto the twelve device-facing config keys
(task E5.7a; spec 5.3, 5.4, 16.2, 16.4; phase-5 fixed choice 3).

An operator configures services in one vocabulary (`influx.url`, `s3.bucket`)
and a device reads them in another (`telemetry.influx_url`,
`upload.s3_bucket`). This module is the translation, and it is a **pure
function of the rows**: same rows in, same twelve keys out, every time.

## Why a projection rather than a second resolution source

The rejected alternative was layering the service rows into the merge chain as
an extra source below `entity_override`. It fails on four counts that compound
(fixed choice 3): it edits `app/config/merge.py`, one of the four spec 14.5
test-critical components; six existing consumers compose the chain from
`entity_override` and would each need to learn about it; the D51 secret-marker
handling already exists and would need a second answer; and `snapshot_from_raw`
was written on the assumption these keys arrive as override values.

So the service rows stay the source of truth and the override row is a
**derived cache of them**, kept honest by being regenerated wholesale on every
save rather than merged into. Two sources of truth are reconciled by making one
a deterministic function of the other and recomputing it every time.

## Why the secret is duplicated at rest, which looks like an oversight

This function reads each credential from its service-owned SecretStore name
(`deployment:{id}:influx_token`) and hands back the PLAINTEXT, which
`put_overrides` then stores a second time under
`config:deployment:{id}:telemetry.influx_token`. Two ciphertexts, one KEK, both
covered by `rotate_kek`, neither ever in a response.

The alternative — writing a marker that pointed at the service-owned name —
would mean an operator unsetting a config key deletes the service row's
credential out from under the connection tester. The two lifecycles are
independent on purpose. **Do not "fix" this** (fixed choice 3).

## What is deliberately not here

`upload.s3_prefix` is a per-Aggregator operator-writable key (D48) and is NOT
write-restricted, so it is not part of this projection. The `mqtt` row
contributes nothing either: broker coordinates reach a device through E4.6's
bootstrap block in `settings.yaml`, not through retained desired config —
a device that could only learn its broker address over the broker could never
make the first connection.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.config.catalog import CATALOG
from app.models import DeploymentService

#: `(service_key, source) -> catalog key`, where `source` names a field on the
#: service's Pydantic model (E5.2's `schemas.py`), and `secret` says whether
#: the value comes from `secret_names` and SecretStore rather than from
#: `config`.
#:
#: **A table rather than twelve `if` branches**, because the property that
#: matters is checkable: every value here is asserted to be a
#: `write_restricted` catalog key, against `CATALOG` itself and not a copied
#: list, at import time and again in the suite. A thirteenth key added to the
#: catalog without a row here is a key no device will ever receive, and a row
#: here for a key that is not write-restricted would let this function write
#: something an operator is also allowed to write — the exact collision fixed
#: choice 3's "regenerated wholesale" would then silently resolve in the
#: platform's favour on every save.
PROJECTION: tuple[tuple[str, str, str, bool], ...] = (
    # (service_key, field on the service model, catalog key, is_secret)
    ("s3", "bucket", "upload.s3_bucket", False),
    ("s3", "endpoint", "upload.s3_endpoint", False),
    ("s3", "access_key", "upload.s3_access_key", True),
    ("s3", "secret_key", "upload.s3_secret_key", True),
    ("influx", "url", "telemetry.influx_url", False),
    ("influx", "token", "telemetry.influx_token", True),
    ("influx", "database", "telemetry.influx_database", False),
    ("prometheus", "read_url", "telemetry.prometheus_url", False),
    ("prometheus", "remote_write_url", "telemetry.prom_remote_write_url", False),
    ("prometheus", "remote_write_user", "telemetry.prom_remote_write_user", False),
    ("prometheus", "remote_write_password", "telemetry.prom_remote_write_password", True),
    ("grafana", "base_url", "telemetry.grafana_url", False),
)

#: The one projected key that comes from the DEPLOYMENT rather than from a
#: service row (E5.11, D134).
#:
#: A rotation changes only secret values, and a device's desired snapshot
#: carries secret MARKERS — SecretStore names, identical before and after. So
#: without this counter a rotation changes nothing in any snapshot, mints no
#: revision, and tells no device anything, which would make spec 16.3's
#: "rotation is a config revision, not a manual redistribution" false in
#: practice. This is the non-secret thing that changes.
GENERATION_KEY = "services.credentials_generation"

#: Every write-restricted key in the catalog. Read FROM `CATALOG` rather than
#: written out, so a catalog change cannot leave a stale copy here.
RESTRICTED_KEYS: frozenset[str] = frozenset(
    entry.key for entry in CATALOG if entry.write_restricted is not None
)

_PROJECTED_KEYS = frozenset(key for _, _, key, _ in PROJECTION) | {GENERATION_KEY}

if not _PROJECTED_KEYS <= RESTRICTED_KEYS:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "PROJECTION names a catalog key that is not write_restricted: "
        f"{sorted(_PROJECTED_KEYS - RESTRICTED_KEYS)}. E5 may only write keys the "
        "catalog marks as service-onboarding-owned (phase-5 fixed choice 3)."
    )


def service_settings(
    rows: Iterable[DeploymentService],
    read_secret: Callable[[str], str],
    *,
    generation: int | None = None,
) -> dict[str, Any]:
    """The twelve keys, as an override map ready for `put_overrides`.

    `read_secret` is `SecretStore.get` rather than the store itself, so this
    function is trivially testable and cannot reach any other part of the
    store's interface. Secrets come back as PLAINTEXT because that is what
    `put_overrides` takes; it is what converts them to markers (D51).

    **A key whose value is absent is absent from the result**, not present as
    null. `app/config/validation.py` rejects an explicit null with "remove the
    key to unset it", and the wholesale regeneration is what makes omission
    mean unset here: an operator who clears the S3 endpoint gets a projection
    with no `upload.s3_endpoint`, and the deployment override loses the key
    rather than keeping the old value forever.

    **A credential whose ciphertext has gone missing is skipped with the key
    absent**, not raised on. One unreadable secret must not stop a services
    save from delivering the other eleven keys — the same rule
    `load_broker_coordinates` follows for a broker row (D64), and the
    connection tester is what tells the operator their credential is broken.
    """
    by_key = {row.service_key: row for row in rows}
    out: dict[str, Any] = {}
    for service_key, field, catalog_key, is_secret in PROJECTION:
        row = by_key.get(service_key)
        if row is None:
            continue
        if is_secret:
            name = row.secret_names.get(field)
            if name is None:
                continue
            try:
                out[catalog_key] = read_secret(name)
            except Exception:  # noqa: BLE001 - SecretStoreError and anything below it
                continue
            continue
        value = row.config.get(field)
        if value is not None:
            out[catalog_key] = value
    # Omitted rather than defaulted when the caller did not pass one: the
    # catalog's own default is 0, and writing a 0 the caller did not mean would
    # be a projection asserting something about a deployment it was not told
    # about. Every caller that projects for delivery passes it.
    if generation is not None:
        out[GENERATION_KEY] = generation
    return out


def projected_keys(settings: Mapping[str, Any]) -> frozenset[str]:
    """The keys a projection covered. Named so tests read as prose."""
    return frozenset(settings)
