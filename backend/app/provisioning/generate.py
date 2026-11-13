"""Bundle file generation (tasks E4.3, E4.4; spec 8.2, 8.5; phase-4 §2).

E4.4 adds spec 8.2 mode 2 (one MAC-keyed file per Listener) and the mixed mode
spec 8.2 permits per Pod: the Pod's shared file for most cards, plus
per-listener files for "the few devices that need" distinct first-boot
settings. See `generate_for_pod` and `generate_for_deployment` at the bottom.

E4.3 is spec 8.2 mode 1: **one file for a Pod, written to every card in it.**
It works because every Listener in a Pod shares the Pod's network
configuration. The per-listener data (name, GPS, MAC) is left blank and filled
in later from the cloud, matched by the MAC the device reports on first
contact (spec 8.2, 8.6).

## Where the values come from, and why that accessor

**`effective_resolved` is the only accessor that can build a card**, and it is
called with the POD as the target. Of the three accessors (D53):

- `effective_for` returns redacted markers;
- `effective_raw` returns secret markers verbatim;
- `effective_resolved` substitutes plaintext from SecretStore.

A card carrying `__secret__` or a marker instead of a PSK boots nothing, so the
choice is load-bearing. The plaintext lives only in memory inside this module,
and nothing here is ever returned over HTTP (INTERFACES, merge engine section).
In v1 the two secrets are dropped by the renderer anyway; E4.5 seals them under
the firmware envelope before they touch a byte.

Resolving at the Pod rather than at each Listener is what makes the file
identical for every card **by construction**: a Listener-level override cannot
reach a per-pod file. A device that needs its own first-boot settings is what
per-listener mode (E4.4) is for.

## The records

Each card gets a planned `device_provisioning_record`, carrying the device's
current `config_revision` at generation time: its latest revision by
`(created_at, id)`, the same total order E3's
`publisher.newest_revision_for_target` uses. **This module never creates a
revision** (phase-4 §2). A device with none is recorded with NULLs, and E4.9
decides what that means for confirmation. Records are only PLANNED here; E4.8
persists them with the bundle row.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.catalog import CATALOG
from app.config.service import effective_resolved
from app.models import BUNDLE_MODES, Aggregator, ConfigRevision, Listener, Pod
from app.provisioning.device_config import render_device_config_v1
from app.secrets import SecretStore

#: Spec 8.2: "The unique per-listener data … is left blank on the card." Derived
#: from the catalog's `resolution="inventory"` flag rather than listed here.
INVENTORY_KEYS: frozenset[str] = frozenset(
    entry.key for entry in CATALOG if entry.resolution == "inventory"
)

#: The Pod's network block. Every key must resolve to a value, or the card
#: cannot join the network it exists to join (E4.3 acceptance).
NETWORK_KEYS: tuple[str, ...] = tuple(
    entry.key for entry in CATALOG if entry.key.startswith("network.")
)

#: `network.wifi_password` is the one network key an OPEN network does not need.
OPEN_SECURITY = "OPEN"


class GenerationError(ValueError):
    """A bundle that would not work in the field, refused before it is made.

    `missing` names the catalog keys that need values, so the caller can tell
    the operator exactly what to set instead of "generation failed".
    """

    def __init__(self, message: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing = missing


@dataclass(frozen=True)
class PlannedRecord:
    """One `device_provisioning_record`, before it has a bundle to belong to."""

    target_type: str
    target_id: str
    pod_id: uuid.UUID
    aggregator_uuid: str
    file_kind: str
    file_path: str
    config_revision_id: uuid.UUID | None
    config_checksum: str | None


@dataclass(frozen=True)
class GeneratedFile:
    """One archive member and the cards that take it."""

    path: str
    content: bytes
    records: tuple[PlannedRecord, ...]


def pod_folder(pod_id: uuid.UUID) -> str:
    """Every file for one Pod lives under this folder in the archive."""
    return f"pods/{pod_id}"


def pod_file_path(pod_id: uuid.UUID) -> str:
    return f"{pod_folder(pod_id)}/device_config.yaml"


def missing_network_keys(values: Mapping[str, Any]) -> tuple[str, ...]:
    """The network keys a working card still needs. An empty string counts as
    unset: a blank SSID is no more joinable than a missing one."""
    open_network = values.get("network.wifi_security") == OPEN_SECURITY
    return tuple(
        key
        for key in NETWORK_KEYS
        if values.get(key) in (None, "") and not (open_network and key == "network.wifi_password")
    )


def current_revision(db: Session, target_type: str, target_id: str) -> ConfigRevision | None:
    """The device's latest revision by `(created_at, id)`, E3's total order."""
    return db.scalars(
        select(ConfigRevision)
        .where(ConfigRevision.target_type == target_type, ConfigRevision.target_id == target_id)
        .order_by(ConfigRevision.created_at.desc(), ConfigRevision.id.desc())
        .limit(1)
    ).first()


def resolved_values(
    db: Session, secret_store: SecretStore, entity_type: str, entity_id: str
) -> dict[str, Any]:
    """Plain values for one entity, secrets in PLAINTEXT. Internal only: the
    result goes to a renderer or an envelope, never to a response."""
    resolved = effective_resolved(db, entity_type, entity_id, secret_store)
    return {key: item.value for key, item in resolved.items()}


def _pod_with_aggregator(db: Session, pod_id: uuid.UUID) -> tuple[Pod, Aggregator]:
    pod = db.get(Pod, pod_id)
    if pod is None:
        raise LookupError(f"pod {pod_id} not found")
    aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod_id))
    if aggregator is None:
        # A card's target slot is its Aggregator (spec 8.5), and a Pod with no
        # Aggregator has no one for its Listeners to stream to.
        raise GenerationError(f"pod {pod.name!r} has no aggregator to provision against")
    return pod, aggregator


def pod_listeners(db: Session, aggregator: Aggregator) -> list[Listener]:
    return list(
        db.scalars(
            select(Listener).where(Listener.aggregator_id == aggregator.id).order_by(Listener.mac)
        )
    )


def listener_record(
    db: Session,
    listener: Listener,
    pod_id: uuid.UUID,
    aggregator_uuid: str,
    *,
    kind: str,
    path: str,
) -> PlannedRecord:
    revision = current_revision(db, "listener", listener.mac)
    return PlannedRecord(
        target_type="listener",
        target_id=listener.mac,
        pod_id=pod_id,
        aggregator_uuid=aggregator_uuid,
        file_kind=kind,
        file_path=path,
        config_revision_id=revision.id if revision is not None else None,
        config_checksum=revision.checksum if revision is not None else None,
    )


def generate_pod_file(
    db: Session,
    secret_store: SecretStore,
    pod_id: uuid.UUID,
    *,
    exclude_macs: frozenset[str] = frozenset(),
) -> GeneratedFile:
    """Spec 8.2 mode 1: the Pod's one shared card file and a record per card.

    `exclude_macs` leaves Listeners out of this file's records because they
    take a per-listener file instead (mixed mode, E4.4). The file's BYTES do
    not change: it is the Pod's file either way.
    """
    pod, aggregator = _pod_with_aggregator(db, pod_id)
    values = resolved_values(db, secret_store, "pod", str(pod.id))

    missing = missing_network_keys(values)
    if missing:
        raise GenerationError(
            f"pod {pod.name!r} cannot join its network: set {', '.join(missing)}",
            missing=missing,
        )

    content = render_device_config_v1(values, blank=INVENTORY_KEYS)
    path = pod_file_path(pod.id)
    records = tuple(
        listener_record(
            db, listener, pod.id, aggregator.aggregator_uuid, kind="pod_file", path=path
        )
        for listener in pod_listeners(db, aggregator)
        if listener.mac not in exclude_macs
    )
    return GeneratedFile(path=path, content=content, records=records)


# --- E4.4: per-listener files and mixed mode ---------------------------------


def listener_file_path(pod_id: uuid.UUID, mac: str) -> str:
    """`pods/<pod_id>/listeners/02-EE-0E-01-01-07.yaml`.

    Dashes, not colons: `:` is illegal in a Windows filename, and cards are
    written on Windows field laptops (phase-4 fixed choice 4). The MAC inside
    the file keeps its canonical colon form.
    """
    return f"{pod_folder(pod_id)}/listeners/{mac.replace(':', '-')}.yaml"


def generate_listener_file(
    db: Session,
    secret_store: SecretStore,
    listener: Listener,
    pod_id: uuid.UUID,
    aggregator_uuid: str,
) -> GeneratedFile:
    """Spec 8.2 mode 2: one Listener's own card, resolved AT THE LISTENER.

    Unlike the per-pod file, nothing is blanked: identity and location are
    this device's own, and a Listener-level override is exactly the
    "distinct settings at first boot" this mode exists for.
    """
    values = resolved_values(db, secret_store, "listener", listener.mac)
    missing = missing_network_keys(values)
    if missing:
        raise GenerationError(
            f"listener {listener.mac} cannot join its network: set {', '.join(missing)}",
            missing=missing,
        )
    path = listener_file_path(pod_id, listener.mac)
    record = listener_record(db, listener, pod_id, aggregator_uuid, kind="listener_file", path=path)
    return GeneratedFile(path=path, content=render_device_config_v1(values), records=(record,))


def generate_for_pod(
    db: Session,
    secret_store: SecretStore,
    pod_id: uuid.UUID,
    mode: str,
    listener_macs: frozenset[str] = frozenset(),
) -> list[GeneratedFile]:
    """Every card file for one Pod in the chosen mode.

    - `per_pod`: the shared file only; `listener_macs` must be empty.
    - `per_listener`: one file per Listener in the Pod; `listener_macs` must be
      empty (the mode means all of them).
    - `mixed`: the shared file for every Listener NOT named, plus a
      per-listener file for exactly the named ones. At least one must be named,
      or it is just `per_pod`.

    A named MAC outside this Pod is refused at generation rather than silently
    included or dropped (E4.4 acceptance): a card generated for the wrong Pod
    would carry the wrong network.
    """
    if mode not in BUNDLE_MODES:
        raise GenerationError(f"unknown mode {mode!r}; expected one of {', '.join(BUNDLE_MODES)}")
    pod, aggregator = _pod_with_aggregator(db, pod_id)
    listeners = pod_listeners(db, aggregator)
    in_pod = {listener.mac: listener for listener in listeners}

    outsiders = sorted(listener_macs - in_pod.keys())
    if outsiders:
        raise GenerationError(f"not listeners of pod {pod.name!r}: {', '.join(outsiders)}")
    if mode in ("per_pod", "per_listener") and listener_macs:
        raise GenerationError(f"mode {mode!r} takes no listener list; use 'mixed' to name devices")
    if mode == "mixed" and not listener_macs:
        raise GenerationError("mode 'mixed' needs at least one listener; otherwise use 'per_pod'")

    files: list[GeneratedFile] = []
    if mode in ("per_pod", "mixed"):
        files.append(generate_pod_file(db, secret_store, pod.id, exclude_macs=listener_macs))
    chosen = listeners if mode == "per_listener" else [in_pod[mac] for mac in sorted(listener_macs)]
    files.extend(
        generate_listener_file(db, secret_store, listener, pod.id, aggregator.aggregator_uuid)
        for listener in chosen
    )
    return files


def generate_for_deployment(
    db: Session,
    secret_store: SecretStore,
    deployment_id: uuid.UUID,
    plan: Mapping[uuid.UUID, tuple[str, frozenset[str]]],
) -> list[GeneratedFile]:
    """A Deployment bundle: each named Pod in its own mode (spec 8.2, "Operators
    pick per Pod"). `plan` maps pod id → (mode, listener MACs). Every Pod must
    belong to this Deployment, so a plan cannot reach across deployments."""
    if not plan:
        raise GenerationError("a deployment bundle needs at least one pod")
    pods = {pod.id: pod for pod in db.scalars(select(Pod).where(Pod.id.in_(list(plan))))}
    foreign = sorted(
        str(pod_id)
        for pod_id in plan
        if pod_id not in pods or pods[pod_id].deployment_id != deployment_id
    )
    if foreign:
        raise GenerationError(f"pods not in deployment {deployment_id}: {', '.join(foreign)}")
    files: list[GeneratedFile] = []
    for pod_id in sorted(plan, key=str):
        mode, macs = plan[pod_id]
        files.extend(generate_for_pod(db, secret_store, pod_id, mode, macs))
    return files
