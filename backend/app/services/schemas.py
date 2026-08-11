"""The typed write boundary for the five spec 16.2 services (task E5.2).

`deployment_service.config` is JSONB and `secret_names` is JSONB, and E5.1
said why: fifteen nullable columns whose validity is a function of
`service_key` would document nothing. **This module is where that typing
actually happens** (phase-5 fixed choice 1, rule R2) - one Pydantic model per
service, `extra="forbid"` on every one of them, so a misspelled field is a
422 rather than a key that silently never reaches a device.

**Secret fields are write-only** (spec 16.2's last sentence, spec 13). Each
accepts three things and nothing else:

* a **plaintext string** - stored in SecretStore under a deterministic name
  and never echoed;
* the **D51 keep sentinel** `{"$secret_set": true}` - keep what is stored,
  which is what a GET renders, so a form the operator never held the secrets
  for round-trips through PUT unchanged;
* **omission or null** - the credential is unset and its SecretStore entry is
  orphaned for post-commit deletion.

The sentinel is `app.config.validation.KEEP_SENTINEL`, reused rather than
reinvented: the config editor already round-trips secrets this way, and a
second wire shape for the same idea is how a frontend ends up with two
secret-handling code paths that drift.

**Nothing here touches the database.** `plan_write` is pure - it takes the
submitted settings plus the row that exists today and returns what to write,
what to put in SecretStore and what to delete after the commit. The API layer
executes the plan; the D51 ordering rule (deletes only after the commit) is
enforced there because only the caller knows when its transaction committed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config.validation import KEEP_SENTINEL
from app.models import SERVICE_KEYS, DeploymentService


class ServiceSettingsError(Exception):
    """One rejected field, folded into the D8 validation_error envelope by
    the API layer. Carries the service and field so the S5 wizard can put the
    message beside the input that caused it."""

    def __init__(self, service_key: str, field: str, code: str, message: str) -> None:
        super().__init__(f"{service_key}.{field}: {message}")
        self.service_key = service_key
        self.field = field
        self.code = code
        self.message = message


class KeepSecret(BaseModel):
    """The D51 keep sentinel as a wire type: `{"$secret_set": true}`.

    A model rather than a `dict[str, Any]` check so the boundary rejects a
    near-miss (`{"$secret_set": false}`, an extra key) with a field-located
    422 instead of quietly treating it as a plaintext value whose text
    happens to be a JSON object.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    secret_set: Literal[True] = Field(alias="$secret_set")


#: What a secret field accepts on the wire. `None` means "unset it" - both an
#: omitted field and an explicit null, deliberately the same, because PUT is
#: never a merge (the E1.7 tags precedent) and a form that clears an input
#: submits neither.
SecretIn = str | KeepSecret | None


def secret_name(deployment_id: object, service_key: str, field: str) -> str:
    """The SecretStore name for one service credential.

    `deployment:{id}:mqtt_password` is exactly what `devbroker.secret_name`
    already mints for the broker account, so Path A rewriting broker
    credentials through this API lands on the same name the control plane
    reads - by construction, not by coincidence. A test pins the two together.
    """
    return f"deployment:{deployment_id}:{service_key}_{field}"


class ServiceSettings(BaseModel):
    """Base for the five. Subclasses declare which of their fields are
    secrets and which live in `deployment_service`'s dedicated columns rather
    than in `config`."""

    model_config = ConfigDict(extra="forbid")

    #: The `service_key` this model writes. Also the registry key.
    service_key: ClassVar[str]
    #: Field names whose values are credentials: SecretStore, never a column.
    secret_fields: ClassVar[tuple[str, ...]] = ()
    #: Field names that map to real columns on `deployment_service` instead
    #: of into the `config` JSONB. Only `mqtt` has any - E5.1 fixed choice 1
    #: kept the six MQTT-shaped columns exactly where E3 put them.
    column_fields: ClassVar[tuple[str, ...]] = ()
    #: The one secret that lives in the `password_secret_name` COLUMN rather
    #: than in the `secret_names` map, because `load_broker_coordinates`
    #: reads that column and E5 does not get to move it.
    secret_column_field: ClassVar[str | None] = None


class MqttSettings(ServiceSettings):
    """Mosquitto (spec 16.2 row 1). The four fields under the
    `mqtt_coordinates_required` CHECK are required here too - but the CHECK
    is what makes it true, and this model is the error message."""

    service_key: ClassVar[str] = "mqtt"
    secret_fields: ClassVar[tuple[str, ...]] = ("password",)
    column_fields: ClassVar[tuple[str, ...]] = (
        "host",
        "port",
        "tls_enabled",
        "ca_cert_pem",
        "username",
    )
    secret_column_field: ClassVar[str | None] = "password"

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    tls_enabled: bool = True
    #: The trust anchor, deliberately NOT a secret (E5.1's model docstring):
    #: it is the public certificate the platform must trust to verify the
    #: broker's TLS identity.
    ca_cert_pem: str | None = None
    username: str = Field(min_length=1, max_length=200)
    #: REQUIRED, and typed without `None` on purpose: the
    #: `mqtt_coordinates_required` CHECK makes `password_secret_name` NOT NULL
    #: for an mqtt row, so an unset password is an IntegrityError the
    #: catch-all would turn into a 500. The boundary answers it as a 422.
    password: str | KeepSecret


class InfluxSettings(ServiceSettings):
    """InfluxDB 3 (spec 16.2 row 2): URL, admin token, database name."""

    service_key: ClassVar[str] = "influx"
    secret_fields: ClassVar[tuple[str, ...]] = ("token",)

    url: str = Field(min_length=1, max_length=500)
    database: str = Field(min_length=1, max_length=200)
    token: SecretIn = None


class PrometheusSettings(ServiceSettings):
    """Prometheus (spec 16.2 row 3): read URL, remote-write URL, basic auth.

    Two URLs rather than one because they are genuinely two endpoints with
    two roles - the platform queries the read URL and the Aggregators' agents
    push to the remote-write URL - and E5.4c's whole job is telling the
    failure modes of the second apart from the first.
    """

    service_key: ClassVar[str] = "prometheus"
    secret_fields: ClassVar[tuple[str, ...]] = ("remote_write_password",)

    read_url: str = Field(min_length=1, max_length=500)
    remote_write_url: str = Field(min_length=1, max_length=500)
    remote_write_user: str = Field(min_length=1, max_length=200)
    remote_write_password: SecretIn = None


class GrafanaSettings(ServiceSettings):
    """Grafana (spec 16.2 row 4): base URL and a service account token."""

    service_key: ClassVar[str] = "grafana"
    secret_fields: ClassVar[tuple[str, ...]] = ("service_account_token",)

    base_url: str = Field(min_length=1, max_length=500)
    service_account_token: SecretIn = None


class S3Settings(ServiceSettings):
    """Object storage (spec 16.2 row 5): endpoint, region, bucket, keys.

    `endpoint` is optional because real AWS needs none; MinIO and Chameleon's
    object store do. `upload.s3_prefix` is deliberately absent - D48 keeps it
    operator-writable and outside the service-onboarding block.
    """

    service_key: ClassVar[str] = "s3"
    secret_fields: ClassVar[tuple[str, ...]] = ("access_key", "secret_key")

    bucket: str = Field(min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=64)
    endpoint: str | None = Field(default=None, max_length=500)
    access_key: SecretIn = None
    secret_key: SecretIn = None


#: The five models by `service_key`, in the spec 16.2 table's order. Its keys
#: are asserted equal to `models.SERVICE_KEYS` at import time, so a sixth
#: service cannot reach the database without a model to type it.
SERVICE_SCHEMAS: dict[str, type[ServiceSettings]] = {
    model.service_key: model
    for model in (MqttSettings, InfluxSettings, PrometheusSettings, GrafanaSettings, S3Settings)
}

if tuple(SERVICE_SCHEMAS) != SERVICE_KEYS:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "SERVICE_SCHEMAS and models.SERVICE_KEYS disagree; every spec 16.2 service "
        "needs a Pydantic model at the write boundary (phase-5 fixed choice 1)."
    )


@dataclass(frozen=True)
class ServiceWritePlan:
    """What one service's save does, decided before anything is executed.

    Split this way so the D51 ordering is the API layer's to enforce and not
    a rule buried in a function that also writes rows: `secrets_to_put` runs
    before the commit (an orphaned ciphertext nothing points at is harmless),
    `secrets_to_delete` only after it (deleting first loses the plaintext if
    the transaction rolls back).
    """

    service_key: str
    config: dict[str, Any]
    columns: dict[str, Any]
    secret_names: dict[str, str]
    password_secret_name: str | None
    secrets_to_put: dict[str, str]
    secrets_to_delete: tuple[str, ...]
    #: Field NAMES written by this save, for the audit detail. Never values -
    #: that is the whole point of the endpoint (rule R2, spec 16.2).
    written_fields: tuple[str, ...]


def _stored_secret_name(
    row: DeploymentService | None, model: type[ServiceSettings], field: str
) -> str | None:
    """The SecretStore name this field currently uses, or None if unset."""
    if row is None:
        return None
    if field == model.secret_column_field:
        return row.password_secret_name
    return row.secret_names.get(field)


def plan_write(
    settings: ServiceSettings,
    deployment_id: object,
    existing: DeploymentService | None,
) -> ServiceWritePlan:
    """Pure: submitted settings plus today's row in, one save's worth of work
    out. Raises `ServiceSettingsError` for a keep sentinel on a field that has
    nothing stored - a real client bug (a form built from a GET that said the
    field was empty), and silently writing nothing would leave the operator
    believing a credential was saved."""
    model = type(settings)
    key = model.service_key
    config: dict[str, Any] = {}
    columns: dict[str, Any] = {}
    secret_names: dict[str, str] = {}
    secrets_to_put: dict[str, str] = {}
    secrets_to_delete: list[str] = []
    written: list[str] = []
    password_secret_name: str | None = None

    for field in model.model_fields:
        value = getattr(settings, field)
        stored = _stored_secret_name(existing, model, field)

        if field in model.secret_fields:
            name = secret_name(deployment_id, key, field)
            if isinstance(value, KeepSecret):
                if stored is None:
                    raise ServiceSettingsError(
                        key,
                        field,
                        "no_stored_secret",
                        "keep sentinel sent for a credential that is not set; "
                        "submit the value or omit the field",
                    )
                resolved: str | None = stored
            elif value is None:
                resolved = None
                if stored is not None:
                    secrets_to_delete.append(stored)
            else:
                resolved = name
                secrets_to_put[name] = value
                written.append(field)
                # A rename can only happen if the naming scheme changes; when
                # it does, the old ciphertext must not be left behind.
                if stored is not None and stored != name:
                    secrets_to_delete.append(stored)
            if field == model.secret_column_field:
                password_secret_name = resolved
            elif resolved is not None:
                secret_names[field] = resolved
            continue

        written.append(field)
        if field in model.column_fields:
            columns[field] = value
        elif value is not None:
            # None stays OUT of `config` rather than in it as a null: the
            # redacted GET renders a missing key as an absent field, and a
            # stored null would round-trip as an explicit null the model
            # would then have to accept as distinct from omission.
            config[field] = value

    return ServiceWritePlan(
        service_key=key,
        config=config,
        columns=columns,
        secret_names=secret_names,
        password_secret_name=password_secret_name,
        secrets_to_put=secrets_to_put,
        secrets_to_delete=tuple(dict.fromkeys(secrets_to_delete)),
        written_fields=tuple(written),
    )


def redacted_settings(row: DeploymentService) -> dict[str, Any]:
    """One row rendered for a GET: every non-secret field at its stored
    value, every SET secret as the keep sentinel, every unset secret absent.

    **No branch of this function can return a credential**, because it never
    reads SecretStore - it reads the row, and the row holds names.
    """
    model = SERVICE_SCHEMAS[row.service_key]
    out: dict[str, Any] = {}
    for field in model.model_fields:
        if field in model.secret_fields:
            if _stored_secret_name(row, model, field) is not None:
                out[field] = dict(KEEP_SENTINEL)
        elif field in model.column_fields:
            out[field] = getattr(row, field)
        elif field in row.config:
            out[field] = row.config[field]
    return out


def audited_fields(plans: Mapping[str, ServiceWritePlan]) -> dict[str, list[str]]:
    """The audit detail: service key -> field names written. Names only."""
    return {key: list(plan.written_fields) for key, plan in plans.items()}
