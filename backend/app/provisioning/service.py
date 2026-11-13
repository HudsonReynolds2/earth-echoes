"""Bundle orchestration: generate, verify, archive, store, record (task E4.8;
spec 8.3, 8.5, 13; D178).

One call turns an operator's choice into a stored bundle:

1. Resolve the Pods in scope and validate the per-Pod choices.
2. Generate every card file (E4.3/E4.4/E4.5) and each Pod's `settings.yaml`
   (E4.6, which also enforces spec 16.5).
3. Assemble the members with the manifest and README (E4.7), then run
   `verify_members`. A bundle whose manifest disagrees with itself is never
   recorded.
4. Zip it, store the archive **encrypted at rest** in SecretStore under the
   reserved `bundle:{id}:archive` name, and write the bundle row and one
   record per card.

## Why the archive is stored, not re-rendered

E5's stack bundle re-renders on download because its output is a pure function
of stored rows. A provisioning archive cannot be: each export draws a fresh
DEK and fresh nonces (spec 8.4), so a re-render would produce DIFFERENT cards
from the ones whose checksums this bundle records, and possibly from ones
already written. The archive also holds each Aggregator's plaintext broker
login (spec 16.4), which is why it goes through SecretStore rather than into a
bytea column.

## Order, and what a failure leaves

The archive is stored BEFORE the rows are committed. SecretStore commits on
its own session (D143), so if the row write then fails, the stored archive is
deleted again. The opposite order would allow a bundle row whose archive does
not exist, which is a download that 404s for a bundle the board says exists.
A broker credential minted during step 2 is flushed into the caller's
transaction (E5.6's rule), so a refused or failed bundle never leaves a row
claiming a login the archive does not carry.
"""

import base64
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    BUNDLE_MODES,
    Aggregator,
    Deployment,
    DeviceProvisioningRecord,
    Pod,
    ProvisioningBundle,
)
from app.provisioning.archive import build_zip
from app.provisioning.bootstrap import generate_settings_yaml
from app.provisioning.device_config import TEMPLATE_VERSION
from app.provisioning.envelope import FirmwareKey
from app.provisioning.generate import GeneratedFile, GenerationError, generate_for_pod
from app.provisioning.manifest import BundleMeta, bundle_members, sha256, verify_members
from app.secrets import SecretStore
from app.services.credentials import BrokerCredentialProvider


def archive_secret_name(bundle_id: uuid.UUID) -> str:
    """The reserved `bundle:{id}:{key}` SecretStore namespace (phase-4 §2)."""
    return f"bundle:{bundle_id}:archive"


@dataclass(frozen=True)
class PodChoice:
    pod_id: uuid.UUID
    mode: str = "per_pod"
    listener_macs: frozenset[str] = frozenset()


class ArchiveMissing(Exception):
    """A bundle row whose stored archive is gone or does not match its checksum."""


def _scoped_pods(
    db: Session,
    deployment: Deployment,
    scope: str,
    choices: Sequence[PodChoice],
) -> list[PodChoice]:
    """The Pods this bundle covers, each with its mode.

    - `pod` scope: exactly one choice, for a Pod in this Deployment.
    - `deployment` scope: every Pod in the Deployment that has an Aggregator
      (a Pod without one has nothing to provision against). A choice
      overrides that Pod's default `per_pod`, and a choice for a Pod outside
      the Deployment is refused.
    """
    for choice in choices:
        if choice.mode not in BUNDLE_MODES:
            raise GenerationError(f"unknown mode {choice.mode!r}")
    pods = {
        pod.id: pod for pod in db.scalars(select(Pod).where(Pod.deployment_id == deployment.id))
    }
    foreign = sorted(str(c.pod_id) for c in choices if c.pod_id not in pods)
    if foreign:
        raise LookupError(f"pods not in deployment {deployment.name!r}: {', '.join(foreign)}")
    if scope == "pod":
        if len(choices) != 1:
            raise GenerationError("a pod bundle names exactly one pod")
        return list(choices)
    if scope != "deployment":
        raise GenerationError(f"unknown scope {scope!r}")
    with_aggregator = set(
        db.scalars(select(Aggregator.pod_id).where(Aggregator.pod_id.in_(list(pods))))
    )
    if not with_aggregator:
        raise GenerationError(f"deployment {deployment.name!r} has no pod with an aggregator")
    chosen = {choice.pod_id: choice for choice in choices}
    return [
        chosen.get(pod_id, PodChoice(pod_id=pod_id))
        for pod_id in sorted(with_aggregator, key=lambda p: pods[p].name)
    ]


async def create_bundle(
    db: Session,
    session_factory: sessionmaker[Session],
    secret_store: SecretStore,
    provider: BrokerCredentialProvider,
    firmware_key: FirmwareKey,
    *,
    deployment_id: uuid.UUID,
    scope: str,
    choices: Sequence[PodChoice],
    include_credentials: bool,
    actor_user_id: uuid.UUID | None,
) -> tuple[ProvisioningBundle, list[DeviceProvisioningRecord]]:
    """Generate, verify, store and record one bundle. Commits on success.

    Raises `LookupError` (unknown deployment or foreign Pod), `GenerationError`
    (a bundle that would not work), or `BootstrapRefused` (spec 16.5).
    Nothing is stored or recorded when any of them is raised.
    """
    deployment = db.get(Deployment, deployment_id)
    if deployment is None:
        raise LookupError(f"deployment {deployment_id} not found")
    pods = _scoped_pods(db, deployment, scope, choices)

    # Every card file first, every settings.yaml after. Card generation is
    # where the operator-facing refusals live (missing network values, bad
    # MACs), and it touches nothing outside this transaction. Minting a broker
    # login does: it lands on the broker and in SecretStore at once. So nothing
    # is minted until every card in the bundle is known to be generable, and a
    # Deployment bundle refused at its third Pod leaves no orphaned login for
    # its first.
    files: list[GeneratedFile] = []
    for choice in pods:
        files += generate_for_pod(
            db,
            secret_store,
            choice.pod_id,
            choice.mode,
            choice.listener_macs,
            firmware_key=firmware_key,
        )
    for choice in pods:
        files.append(
            await generate_settings_yaml(
                db,
                session_factory,
                secret_store,
                provider,
                choice.pod_id,
                include_credentials=include_credentials,
            )
        )

    bundle_id = uuid.uuid4()
    plan = {
        str(choice.pod_id): {"mode": choice.mode, "listener_macs": sorted(choice.listener_macs)}
        for choice in pods
    }
    pod_names = {
        pod.id: pod.name
        for pod in db.scalars(select(Pod).where(Pod.id.in_([c.pod_id for c in pods])))
    }
    meta = BundleMeta(
        bundle_id=bundle_id,
        deployment_id=deployment.id,
        deployment_name=deployment.name,
        pod_names=pod_names,
        plan=plan,
        template_version=TEMPLATE_VERSION,
        firmware_key_version=firmware_key.key_version,
        bootstrap_credentials=include_credentials,
    )
    members = bundle_members(meta, files)
    problems = verify_members(members)
    if problems:
        # A generator bug, never an operator error: refuse to record a bundle
        # whose own manifest disagrees with it (spec 14.5).
        raise RuntimeError(f"bundle {bundle_id} failed its own manifest: {problems}")
    archive = build_zip(members)

    name = archive_secret_name(bundle_id)
    secret_store.put(name, base64.b64encode(archive).decode("ascii"))
    try:
        now = datetime.now(UTC)
        bundle = ProvisioningBundle(
            id=bundle_id,
            deployment_id=deployment.id,
            scope_type=scope,
            scope_id=pods[0].pod_id if scope == "pod" else deployment.id,
            plan=plan,
            template_version=TEMPLATE_VERSION,
            firmware_key_version=firmware_key.key_version,
            archive_checksum=sha256(archive),
            generated_by=actor_user_id,
            generated_at=now,
        )
        db.add(bundle)
        db.flush()
        actor = str(actor_user_id) if actor_user_id is not None else None
        records = [
            DeviceProvisioningRecord(
                bundle_id=bundle.id,
                deployment_id=deployment.id,
                pod_id=planned.pod_id,
                aggregator_uuid=planned.aggregator_uuid,
                target_type=planned.target_type,
                target_id=planned.target_id,
                file_kind=planned.file_kind,
                file_path=planned.file_path,
                config_revision_id=planned.config_revision_id,
                config_checksum=planned.config_checksum,
                status="generated",
                status_history=[{"status": "generated", "at": now.isoformat(), "actor": actor}],
            )
            for generated in files
            for planned in generated.records
        ]
        db.add_all(records)
        db.commit()
    except BaseException:
        db.rollback()
        secret_store.delete(name)
        raise
    return bundle, records


def load_archive(secret_store: SecretStore, bundle: ProvisioningBundle) -> bytes:
    """The stored archive, refused unless it still matches the recorded
    checksum: a download must be exactly the bundle the board describes."""
    name = archive_secret_name(bundle.id)
    if not secret_store.exists(name):
        raise ArchiveMissing(f"bundle {bundle.id} has no stored archive")
    archive = base64.b64decode(secret_store.get(name))
    if sha256(archive) != bundle.archive_checksum:
        raise ArchiveMissing(f"bundle {bundle.id}'s stored archive does not match its checksum")
    return archive
