"""E5.9: generate a stack's credentials and register them, in one transaction.

Fixed choice 7, stated as an order of operations: **every credential is
generated, stored and committed BEFORE a single byte of the bundle is
rendered.** There is no blob column, no temp directory and no cleanup job, and
therefore no window in which a bundle exists whose credentials the platform
cannot verify. A download re-renders from the stored rows, which is why two
downloads are byte-identical.

**Nothing is persisted that a render needs and cannot find.** The five
`deployment_service` rows plus a handful of deterministically-named secrets are
the whole of it — no `deployment_stack` table, because fixed choice 7 says the
download "re-renders deterministically from those rows" and a second store of
generation parameters would be a second thing to keep in step. Concretely:
whether object storage was included is *whether an `s3` row exists*; the
broker's hostname is the `mqtt` row's `host`; the CA is its `ca_cert_pem`.

**On atomicity, honestly.** `SecretStore.put` opens its own session and commits
(E0.11), so secrets do NOT roll back with the row transaction. The acceptance
this unit is held to — a fault before commit leaves zero rows and zero secrets
— is met by compensation rather than by a shared transaction: the secrets are
written first, and if anything afterwards raises, every name written by this
call is deleted before the error propagates. That ordering is deliberate and
matches D51's: an orphaned ciphertext nothing points at is harmless, while
deleting before a commit that then rolls back destroys a live credential. What
compensation cannot cover is the process being killed between the puts and the
rollback, which leaves unreferenced ciphertext and no rows — harmless, and
named here so the next reader does not mistake it for an oversight.
"""

import logging
import secrets as secretslib
import uuid
from dataclasses import dataclass

import bcrypt
from sqlalchemy.orm import Session

from app import brokerconfig
from app.models import Deployment, DeploymentService
from app.secrets import SecretStore
from app.services import store
from app.services.schemas import secret_name
from app.services.stack import INFLUX_DATABASE, PORTS, StackSecrets, StackSpec

log = logging.getLogger(__name__)

#: 32 bytes of `secrets.token_urlsafe` entropy per credential. Deliberately
#: not derived from the slug, id or name of anything: a credential a reader of
#: the inventory could reconstruct is not a credential, and E5.9's acceptance
#: diffs two generations for one deployment to prove it.
TOKEN_BYTES = 32

#: bcrypt work factor for the Prometheus password. 12 is the current sensible
#: default; this hash is verified on every scrape, so a much higher factor buys
#: little against a credential of this entropy and costs real CPU.
BCRYPT_ROUNDS = 12


#: The stack's own material, under names a re-render can find without a table
#: to look them up in. Distinct from the per-service secret names E5.2 writes
#: (`deployment:{id}:{service}_{field}`) so that an operator saving service
#: settings by hand cannot overwrite the material the bundle is rendered from.
def stack_secret_name(deployment_id: object, item: str) -> str:
    return f"deployment:{deployment_id}:stack:{item}"


#: Everything `stack_secret_name` is called with. Enumerated so regeneration
#: and deletion cannot miss one by being written in a different place from the
#: generator — the failure that leaves a rotated deployment holding a live old
#: credential.
STACK_SECRET_ITEMS = (
    "ca_cert",
    "ca_key",
    "server_cert",
    "server_key",
    "broker_admin_password",
    "broker_admin_password_hash",
    "prometheus_password_hash",
)


class StackNotGenerated(Exception):
    """Raised when a render is asked for and no stack was ever generated."""


@dataclass(frozen=True)
class GeneratedStack:
    """What generation produced, for the caller that is about to render it."""

    spec: StackSpec
    #: Written into the bundle's `.env`; the compose file interpolates them
    #: rather than embedding them in the provisioning YAML.
    env: dict[str, str]


def _token() -> str:
    return secretslib.token_urlsafe(TOKEN_BYTES)


def bcrypt_hash(password: str) -> str:
    """Prometheus's `web_config.yml` accepts bcrypt and nothing else.

    Salted once here, at generation time, for the same reason the broker's
    dynsec hash is (D130): re-hashing at render time would re-salt and break
    the byte-identical download that fixed choice 7 rests on.
    """
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def generate_stack(
    db: Session,
    secret_store: SecretStore,
    deployment: Deployment,
    *,
    include_object_storage: bool = False,
    hostnames: tuple[str, ...] = ("localhost",),
    ips: tuple[str, ...] = ("127.0.0.1",),
    platform_base_url: str = "http://host.docker.internal:8000",
) -> GeneratedStack:
    """Generate every credential, store it, and write the service rows.

    Commits. On any failure every secret this call wrote is deleted and the
    rows are rolled back, so the deployment is left exactly as it was found.

    Regeneration (E5.11 rotation) is the same call: it overwrites each secret
    name and rewrites each row, and `secrets_to_delete` covers names that no
    longer apply — notably object storage's, when a regeneration turns it off.
    """
    deployment_id = deployment.id
    #: Names that did not exist before this call — deleted if it fails.
    created: list[str] = []
    #: Names that DID exist, with their previous values — restored if it fails.
    #:
    #: **Regeneration overwrites the same deterministic names**, so a
    #: compensation that only deleted would destroy the working credentials of
    #: the stack it was trying to replace. A failed rotation has to leave the
    #: deployment exactly as it was found, which means putting the old value
    #: back, not removing the name. Caught by the test rather than foreseen.
    replaced: dict[str, str] = {}
    seen: set[str] = set()

    def put(name: str, value: str) -> None:
        if name not in seen:
            seen.add(name)
            if secret_store.exists(name):
                replaced[name] = secret_store.get(name)
            else:
                created.append(name)
        secret_store.put(name, value)

    try:
        # --- generate ---------------------------------------------------
        broker_admin_password = _token()
        broker_admin_hash = brokerconfig.dynsec_password_hash(broker_admin_password)
        influx_token = _token()
        prometheus_password = _token()
        prometheus_hash = bcrypt_hash(prometheus_password)
        grafana_password = _token()
        minio_user = "eoe-stack"
        minio_password = _token()

        tls = brokerconfig.generate_tls_material(hostnames=hostnames, ips=ips)
        ca_cert_pem = tls["ca.crt"].decode()

        broker_username = brokerconfig.platform_username(deployment.slug)

        # --- store, before anything is rendered -------------------------
        put(stack_secret_name(deployment_id, "ca_cert"), ca_cert_pem)
        put(stack_secret_name(deployment_id, "ca_key"), tls["ca.key"].decode())
        put(stack_secret_name(deployment_id, "server_cert"), tls["server.crt"].decode())
        put(stack_secret_name(deployment_id, "server_key"), tls["server.key"].decode())
        put(stack_secret_name(deployment_id, "broker_admin_password"), broker_admin_password)
        put(stack_secret_name(deployment_id, "broker_admin_password_hash"), broker_admin_hash)
        put(stack_secret_name(deployment_id, "prometheus_password_hash"), prometheus_hash)

        mqtt_password_name = secret_name(deployment_id, "mqtt", "password")
        influx_token_name = secret_name(deployment_id, "influx", "token")
        prom_password_name = secret_name(deployment_id, "prometheus", "remote_write_password")
        grafana_token_name = secret_name(deployment_id, "grafana", "service_account_token")
        put(mqtt_password_name, broker_admin_password)
        put(influx_token_name, influx_token)
        put(prom_password_name, prometheus_password)
        # Grafana has no API token until someone creates one; the admin
        # password is what the stack ships with, and E5.4d's tester dials the
        # health and datasource endpoints with it.
        put(grafana_token_name, grafana_password)

        if include_object_storage:
            s3_access_name = secret_name(deployment_id, "s3", "access_key")
            s3_secret_name_ = secret_name(deployment_id, "s3", "secret_key")
            put(s3_access_name, minio_user)
            put(s3_secret_name_, minio_password)

        # --- write the rows ---------------------------------------------
        host = hostnames[0]
        store.upsert_service(
            db,
            deployment_id,
            "mqtt",
            host=host,
            port=PORTS["mosquitto"],
            tls_enabled=True,
            ca_cert_pem=ca_cert_pem,
            username=broker_username,
            password_secret_name=mqtt_password_name,
        )
        store.upsert_service(
            db,
            deployment_id,
            "influx",
            config={"url": f"http://{host}:{PORTS['influx']}", "database": INFLUX_DATABASE},
            secret_names={"token": influx_token_name},
        )
        store.upsert_service(
            db,
            deployment_id,
            "prometheus",
            config={
                "read_url": f"http://{host}:{PORTS['prometheus']}",
                "remote_write_url": f"http://{host}:{PORTS['prometheus']}/api/v1/write",
                "remote_write_user": "eoe",
            },
            secret_names={"remote_write_password": prom_password_name},
        )
        store.upsert_service(
            db,
            deployment_id,
            "grafana",
            config={"base_url": f"http://{host}:{PORTS['grafana']}"},
            secret_names={"service_account_token": grafana_token_name},
        )
        if include_object_storage:
            store.upsert_service(
                db,
                deployment_id,
                "s3",
                config={
                    "bucket": "echoes",
                    "region": "us-east-1",
                    "endpoint": f"http://{host}:{PORTS['minio']}",
                },
                secret_names={
                    "access_key": secret_name(deployment_id, "s3", "access_key"),
                    "secret_key": secret_name(deployment_id, "s3", "secret_key"),
                },
            )

        # A generated stack has been verified by nobody yet. Spec 16.5 gates
        # bundle generation on verification, and starting anywhere but
        # `untested` would let a stack vouch for itself.
        for row in store.load_services(db, deployment_id):
            row.status = "untested"
            row.status_reason = None
            row.consecutive_failures = 0
            row.last_tested_at = None

        _drop_object_storage_if_absent(db, secret_store, deployment_id, include_object_storage)
        db.commit()
    except BaseException:
        db.rollback()
        for name, previous in replaced.items():
            try:
                secret_store.put(name, previous)
            except Exception:  # noqa: BLE001 - compensation must not mask the original
                log.exception("could not restore previous secret %s", name)
        for name in created:
            try:
                secret_store.delete(name)
            except Exception:  # noqa: BLE001 - compensation must not mask the original
                log.exception("could not roll back generated secret %s", name)
        raise

    return _assemble(db, secret_store, deployment, platform_base_url=platform_base_url)


def _drop_object_storage_if_absent(
    db: Session,
    secret_store: SecretStore,
    deployment_id: uuid.UUID,
    include_object_storage: bool,
) -> None:
    """A regeneration that turns object storage OFF must remove its row.

    Left behind, the row keeps `services_status` waiting on a service the
    operator just said they do not run — and `roll_up` reads rows, not
    intentions.
    """
    if include_object_storage:
        return
    row = store.load_service(db, deployment_id, "s3")
    if row is not None:
        db.delete(row)


def _assemble(
    db: Session,
    secret_store: SecretStore,
    deployment: Deployment,
    *,
    platform_base_url: str,
) -> GeneratedStack:
    """Build the render spec from the STORED rows and secrets.

    This is the function that makes two downloads byte-identical: it reads what
    was committed rather than what was generated, so a download taken a month
    later renders from exactly the same inputs.
    """
    rows = {row.service_key: row for row in store.load_services(db, deployment.id)}
    mqtt = rows.get("mqtt")
    if mqtt is None or not secret_store.exists(stack_secret_name(deployment.id, "ca_cert")):
        raise StackNotGenerated(
            f"no generated stack for deployment {deployment.slug!r}; "
            "POST .../services/stack generates one"
        )

    include_object_storage = "s3" in rows
    secrets = StackSecrets(
        broker_admin_username=mqtt.username or brokerconfig.platform_username(deployment.slug),
        broker_admin_password_hash=secret_store.get(
            stack_secret_name(deployment.id, "broker_admin_password_hash")
        ),
        influx_token=_secret_for(secret_store, rows.get("influx"), "token"),
        prometheus_username="eoe",
        prometheus_password_bcrypt=secret_store.get(
            stack_secret_name(deployment.id, "prometheus_password_hash")
        ),
        grafana_admin_username="eoe",
        grafana_admin_password=_secret_for(
            secret_store, rows.get("grafana"), "service_account_token"
        ),
        minio_root_user=(
            _secret_for(secret_store, rows.get("s3"), "access_key")
            if include_object_storage
            else ""
        ),
        minio_root_password=(
            _secret_for(secret_store, rows.get("s3"), "secret_key")
            if include_object_storage
            else ""
        ),
    )
    spec = StackSpec(
        deployment_slug=deployment.slug,
        secrets=secrets,
        hostnames=(mqtt.host or "localhost",),
        include_object_storage=include_object_storage,
        platform_base_url=platform_base_url,
    )
    env = {
        "PROMETHEUS_PASSWORD": _secret_for(
            secret_store, rows.get("prometheus"), "remote_write_password"
        ),
        "INFLUX_TOKEN": secrets.influx_token,
    }
    return GeneratedStack(spec=spec, env=env)


def _secret_for(secret_store: SecretStore, row: DeploymentService | None, field: str) -> str:
    if row is None:
        return ""
    name = row.secret_names.get(field)
    return secret_store.get(name) if name else ""


def load_generated_stack(
    db: Session,
    secret_store: SecretStore,
    deployment: Deployment,
    *,
    platform_base_url: str = "http://host.docker.internal:8000",
) -> GeneratedStack:
    """Re-read a previously generated stack, for `GET .../stack/download`."""
    return _assemble(db, secret_store, deployment, platform_base_url=platform_base_url)


def tls_material(secret_store: SecretStore, deployment_id: uuid.UUID) -> dict[str, str]:
    """The broker's certificates and keys, for writing into the bundle.

    Generated ONCE at POST and stored, rather than regenerated per download —
    a fresh certificate on every download would be a different bundle every
    time, and every already-provisioned Aggregator would stop trusting the
    broker (fixed choice 7 says exactly this).
    """
    return {
        "ca.crt": secret_store.get(stack_secret_name(deployment_id, "ca_cert")),
        "server.crt": secret_store.get(stack_secret_name(deployment_id, "server_cert")),
        "server.key": secret_store.get(stack_secret_name(deployment_id, "server_key")),
    }


def delete_stack_secrets(secret_store: SecretStore, deployment_id: uuid.UUID) -> int:
    """Remove every stack-owned secret. Used by rotation's cleanup and by
    deployment deletion; returns how many existed."""
    removed = 0
    for item in STACK_SECRET_ITEMS:
        name = stack_secret_name(deployment_id, item)
        if secret_store.exists(name):
            secret_store.delete(name)
            removed += 1
    return removed
