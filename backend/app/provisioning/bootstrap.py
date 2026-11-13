"""The Aggregator's `settings.yaml` and its bootstrap block (task E4.6; spec 8.2,
8.3, 16.4, 16.5; phase-4 fixed choice 1; DECISIONS D117, D162, D167, D176).

## Plaintext, on purpose

Spec 16.4 decided this deliberately, and this module restates it so nobody
"improves" it later: the block carries the device's broker username and
password **in plaintext**. The Pi sits outside the Listener firmware envelope,
its storage travels with the device rather than on a card handed around in the
field, the credential is per-device and confined by ACL to its own subtree
(spec 7.1), and a lost Pi is answered by revoking that one login, which cuts
off everything else it could reach. The bootstrap block is the complete set
the device needs to reach the control plane, **and nothing more**: every
service setting and secret arrives later as retained desired config (spec
16.4, post-connect delivery).

## The gate (spec 16.5)

A bundle is refused unless the deployment's `services_status` is `verified`.
E5 reports that status and this module enforces it (D157, corrected to E4.6 by
D162). The deployment must also have a usable MQTT row, read through
`coordinates_for`, which carries D64's and D121's "is this row usable" rules.
Both refusals are `BootstrapRefused`, which E4.8 turns into a 422 naming the
deployment.

## Credentials, and why regeneration does not rotate them

With `EOE_BOOTSTRAP_CREDENTIALS` on (the default, D167), the block embeds the
Aggregator's broker login. An existing `minted` row in `broker_credential` is
**reused**: re-minting would change the password on the broker, and the card
already in that Pi would stop working the moment someone regenerated a bundle
for a sibling. Only an Aggregator with no live login gets one minted, through
E5.6's `mint_credential` and whichever provider `app.state` holds. With the
flag off, the credential fields are absent and `missing:` names them, so the
file is still valid YAML that says what it lacks.

`aggregator_uuid` is read from inventory and never minted here (phase-4 §2):
E1 assigned it at create time, and a second minting path would break the
spec 4.2 join key.
"""

import uuid
from dataclasses import dataclass
from typing import Any

import yaml
from sqlalchemy.orm import Session, sessionmaker

from app.models import Aggregator, Deployment, Pod
from app.provisioning.generate import (
    GeneratedFile,
    PlannedRecord,
    _pod_with_aggregator,
    current_revision,
    pod_folder,
)
from app.secrets import SecretStore
from app.services.credentials import (
    BrokerCredentialProvider,
    CredentialError,
    coordinates_for,
    load_credential,
    mint_credential,
)

#: Bumped only by a new renderer, like `device_config.TEMPLATE_VERSION`.
SETTINGS_SCHEMA_VERSION = 1

#: The credential fields, named in `missing:` when the flag is off.
CREDENTIAL_FIELDS = ("mqtt.username", "mqtt.password")

HEADER = (
    "# Echoes of Earth aggregator settings.yaml: the bootstrap block (spec 16.4).\n"
    "# PLAINTEXT BY DESIGN (spec 16.4). If this Pi is lost, revoke its broker login.\n"
    "# DRAFT pending aggregator-pi sign-off (DECISIONS D176).\n"
)


class BootstrapRefused(Exception):
    """Spec 16.5: no bundle for a deployment whose broker is not verified.

    `deployment_name` is carried so the API's 422 can name the deployment
    (E4.6 acceptance) without the caller looking it up again.
    """

    def __init__(self, message: str, deployment_name: str) -> None:
        super().__init__(message)
        self.deployment_name = deployment_name


@dataclass(frozen=True)
class BrokerBlock:
    """What the device dials, and as whom. Never logged: `password` is the
    one plaintext credential this epic writes to a file."""

    host: str
    port: int
    tls: bool
    ca_cert: str | None
    username: str | None
    password: str | None

    def __repr__(self) -> str:
        return f"BrokerBlock(host={self.host!r}, port={self.port}, username={self.username!r})"


class _BlockDumper(yaml.SafeDumper):
    """A SafeDumper that writes multi-line strings as literal blocks (`|`).

    The CA certificate is the one multi-line value here, and PyYAML's default
    renders it as a single-quoted folded scalar with doubled blank lines: valid,
    but unreadable to the person who opens this file on a Pi and easy to break
    by hand. A literal block is the PEM exactly as it looks anywhere else."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_BlockDumper.add_representer(str, _represent_str)


def settings_yaml_path(pod_id: uuid.UUID) -> str:
    return f"{pod_folder(pod_id)}/aggregator/settings.yaml"


def require_verified(deployment: Deployment) -> None:
    """The spec 16.5 refusal, enforced at exactly one place."""
    if deployment.services_status != "verified":
        raise BootstrapRefused(
            f"deployment {deployment.name!r} cannot be provisioned: its services are "
            f"{deployment.services_status!r}, and spec 16.5 requires 'verified'. Configure "
            "and verify the broker under Services first.",
            deployment_name=deployment.name,
        )


def render_settings_yaml_v1(aggregator_uuid: str, broker: BrokerBlock) -> bytes:
    """Pure renderer. Deterministic for fixed input: no timestamps."""
    mqtt: dict[str, Any] = {
        "host": broker.host,
        "port": broker.port,
        "tls": broker.tls,
        "ca_cert": broker.ca_cert,
    }
    document: dict[str, Any] = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "aggregator_uuid": aggregator_uuid,
    }
    if broker.username is not None and broker.password is not None:
        mqtt |= {"username": broker.username, "password": broker.password}
        document["mqtt"] = mqtt
    else:
        document["mqtt"] = mqtt
        # Still valid YAML, and it says what it lacks (E4.6 acceptance).
        document["missing"] = list(CREDENTIAL_FIELDS)
    body = yaml.dump(
        document,
        Dumper=_BlockDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=1000,
    )
    return (HEADER + body).encode("utf-8")


async def _credentials(
    db: Session,
    secret_store: SecretStore,
    provider: BrokerCredentialProvider,
    coordinates: Any,
    aggregator: Aggregator,
) -> tuple[str, str]:
    row = load_credential(db, coordinates.deployment_id, aggregator.aggregator_uuid)
    if row is None or row.state != "minted":
        row = await mint_credential(
            db, secret_store, provider, coordinates, aggregator.aggregator_uuid
        )
    return row.username, secret_store.get(row.password_secret_name)


async def generate_settings_yaml(
    db: Session,
    session_factory: sessionmaker[Session],
    secret_store: SecretStore,
    provider: BrokerCredentialProvider,
    pod_id: uuid.UUID,
    *,
    include_credentials: bool,
) -> GeneratedFile:
    """The Aggregator's card for one Pod, with its planned record.

    Raises `BootstrapRefused` before anything is minted if the deployment is
    not verified or has no usable broker row. Does not commit: a minted
    credential's row is flushed into the caller's transaction (E5.6's rule).
    """
    pod: Pod
    pod, aggregator = _pod_with_aggregator(db, pod_id)
    deployment = db.get(Deployment, pod.deployment_id)
    assert deployment is not None, "a pod's deployment is a foreign key"
    require_verified(deployment)
    try:
        coordinates = coordinates_for(session_factory, secret_store, deployment.id)
    except CredentialError as error:
        raise BootstrapRefused(
            f"deployment {deployment.name!r} has no usable MQTT broker row: {error}",
            deployment_name=deployment.name,
        ) from error

    username: str | None = None
    password: str | None = None
    if include_credentials:
        username, password = await _credentials(db, secret_store, provider, coordinates, aggregator)
    broker = BrokerBlock(
        host=coordinates.host,
        port=coordinates.port,
        tls=coordinates.tls_enabled,
        ca_cert=coordinates.ca_cert_pem,
        username=username,
        password=password,
    )

    path = settings_yaml_path(pod.id)
    # D75: an Aggregator's revisions are keyed by its PLATFORM id (the row
    # primary key), not by the aggregator_uuid the topic and this file carry.
    revision = current_revision(db, "aggregator", str(aggregator.id))
    record = PlannedRecord(
        target_type="aggregator",
        target_id=str(aggregator.id),
        pod_id=pod.id,
        aggregator_uuid=aggregator.aggregator_uuid,
        file_kind="settings_yaml",
        file_path=path,
        config_revision_id=revision.id if revision is not None else None,
        config_checksum=revision.checksum if revision is not None else None,
    )
    return GeneratedFile(
        path=path,
        content=render_settings_yaml_v1(aggregator.aggregator_uuid, broker),
        records=(record,),
    )
