"""Versioned settings catalog (task E2.1; spec 5.3; DECISIONS D47-D49).

The CATALOG constant below is the single source of truth: the settings_catalog
table is seeded FROM it, in-migration, via the idempotent and convergent
seed_catalog() upsert - so a fresh history replay and an upgraded database
land on identical rows no matter when the constant last changed. Evolving the
catalog = edit the constant + add a sync migration calling seed_catalog() +
bump CATALOG_VERSION, always in one batch (docs/INTERFACES.md, "Owned by E2").

A gate test asserts the constant matches spec 5.3 key for key AND that the
seeded table matches the constant field for field; drift in either direction
is a red gate.
"""

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

CATALOG_VERSION = 1

# Merge order, root first (E2.3 walks this; spec 5.1). "any" in a catalog
# row's lowest_level means the key is meaningful everywhere and behaves as
# "listener" for the level rule (D47).
LEVELS = ("organization", "deployment", "pod", "aggregator", "listener")

# write_restricted marker: the E5 services onboarding flow writes these keys
# (spec 5.3 closing paragraph, spec 16); the generic override PUT rejects
# them until then (D48 - a documented R2 stub, not a missing feature).
SERVICE_ONBOARDING = "service_onboarding"


@dataclass(frozen=True)
class CatalogEntry:
    """One spec-5.3 row. default is JSON-typed; None means no default (the
    notes say which of "none" / "deployment default" / nullable it is)."""

    key: str
    value_type: str  # int | float | bool | string | object
    lowest_level: str  # organization | deployment | pod | aggregator | listener | any
    default: Any = None
    enum_values: tuple[Any, ...] | None = None
    min_value: float | None = None
    max_value: float | None = None
    secret: bool = False
    resolution: str = "override"  # override | inventory
    write_restricted: str | None = None
    notes: str = ""


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        key="audio.sample_rate_hz",
        value_type="int",
        lowest_level="listener",
        default=48000,
        enum_values=(8000, 16000, 32000, 48000, 96000, 192000, 250000, 384000),
    ),
    CatalogEntry(
        key="audio.bits_per_sample",
        value_type="int",
        lowest_level="listener",
        default=16,
        enum_values=(16, 24),
    ),
    CatalogEntry(key="audio.channels", value_type="int", lowest_level="listener", default=1),
    CatalogEntry(
        key="capture.mode",
        value_type="string",
        lowest_level="listener",
        default="duty_cycle",
        enum_values=("continuous", "duty_cycle", "schedule"),
    ),
    CatalogEntry(
        key="capture.duty_on_seconds",
        value_type="int",
        lowest_level="listener",
        default=60,
        notes="applies when mode=duty_cycle",
    ),
    CatalogEntry(
        key="capture.duty_off_seconds", value_type="int", lowest_level="listener", default=0
    ),
    CatalogEntry(
        key="capture.schedule",
        value_type="object",
        lowest_level="listener",
        default=None,
        notes="applies when mode=schedule; cron-like object whose internal "
        "schema is owned by firmware (E4) - the platform stores and "
        "delivers it opaquely",
    ),
    CatalogEntry(
        key="listener.wake_grace_seconds",
        value_type="int",
        lowest_level="aggregator",
        default=30,
        notes="grace period past a Listener's declared wake time before it's "
        "marked offline; overridable at Aggregator, Deployment, or "
        "Organization level",
    ),
    CatalogEntry(
        key="buffering.sd_enabled",
        value_type="bool",
        lowest_level="listener",
        default=True,
        notes="microSD fallback buffering",
    ),
    CatalogEntry(
        key="buffering.sd_max_bytes",
        value_type="int",
        lowest_level="listener",
        default=0,
        notes="0 = unbounded to card",
    ),
    CatalogEntry(
        key="logging.verbosity",
        value_type="string",
        lowest_level="any",
        default="info",
        enum_values=("error", "warn", "info", "debug", "trace"),
        notes="status/log verbosity",
    ),
    CatalogEntry(key="network.wifi_ssid", value_type="string", lowest_level="pod"),
    CatalogEntry(
        key="network.wifi_password",
        value_type="string",
        lowest_level="pod",
        secret=True,
        notes="stored encrypted; never in plaintext export",
    ),
    CatalogEntry(
        key="network.wifi_security",
        value_type="string",
        lowest_level="pod",
        default="WPA3",
        enum_values=("WPA2", "WPA3", "WPA2_ENT", "OPEN"),
    ),
    CatalogEntry(
        key="network.stream_key",
        value_type="string",
        lowest_level="pod",
        secret=True,
        notes="symmetric key encrypting audio and local control traffic "
        "between Listeners and their Aggregator, independent of "
        "WiFi-layer encryption; delivered and rotated per spec 8.4/8.7",
    ),
    CatalogEntry(
        key="network.aggregator_ip",
        value_type="string",
        lowest_level="pod",
        notes="IP address; shared by the Pod's Listeners",
    ),
    CatalogEntry(
        key="network.stream_endpoint",
        value_type="string",
        lowest_level="pod",
        notes="host:port/path; shared by the Pod's Listeners",
    ),
    CatalogEntry(
        key="network.stream_protocol",
        value_type="string",
        lowest_level="pod",
        default="ws",
        enum_values=("ws", "tcp", "udp"),
        notes="shared by the Pod's Listeners",
    ),
    CatalogEntry(
        key="identity.name",
        value_type="string",
        lowest_level="listener",
        resolution="inventory",
        notes="unique within Deployment; resolves from the listener row "
        "(D31/D49) - edited via PATCH /listeners/{mac}, never overrides",
    ),
    CatalogEntry(
        key="identity.mac",
        value_type="string",
        lowest_level="listener",
        resolution="inventory",
        notes="immutable key (spec 4.2); resolves from the listener row",
    ),
    CatalogEntry(
        key="location.gps_lat",
        value_type="float",
        lowest_level="listener",
        resolution="inventory",
        notes="nullable until filled; resolves from the listener row",
    ),
    CatalogEntry(
        key="location.gps_lon",
        value_type="float",
        lowest_level="listener",
        resolution="inventory",
        notes="nullable until filled; resolves from the listener row",
    ),
    CatalogEntry(
        key="analysis.model_id",
        value_type="string",
        lowest_level="aggregator",
        default="birdnet-default",
        notes="edge AI model selection",
    ),
    CatalogEntry(
        key="analysis.confidence_threshold",
        value_type="float",
        lowest_level="aggregator",
        default=0.5,
        min_value=0.0,
        max_value=1.0,
        notes="detection confidence, 0-1",
    ),
    CatalogEntry(
        key="upload.s3_bucket",
        value_type="string",
        lowest_level="deployment",
        write_restricted=SERVICE_ONBOARDING,
        notes="deployment default; written by the services onboarding flow (E5, spec 16)",
    ),
    CatalogEntry(
        key="upload.s3_prefix",
        value_type="string",
        lowest_level="aggregator",
        default="",
        notes="per-aggregator S3 path prefix (spec 5.1); operator-writable, "
        "deliberately outside the service-onboarding block (D48)",
    ),
    CatalogEntry(
        key="upload.s3_endpoint",
        value_type="string",
        lowest_level="deployment",
        write_restricted=SERVICE_ONBOARDING,
        notes="endpoint URL for non-AWS object stores (MinIO, Chameleon "
        "Object Store); deployment default",
    ),
    CatalogEntry(
        key="upload.s3_access_key",
        value_type="string",
        lowest_level="deployment",
        secret=True,
        write_restricted=SERVICE_ONBOARDING,
        notes="delivered to Aggregators post-connect only (spec 16.4)",
    ),
    CatalogEntry(
        key="upload.s3_secret_key",
        value_type="string",
        lowest_level="deployment",
        secret=True,
        write_restricted=SERVICE_ONBOARDING,
        notes="delivered to Aggregators post-connect only (spec 16.4)",
    ),
    CatalogEntry(
        key="telemetry.influx_url",
        value_type="string",
        lowest_level="deployment",
        write_restricted=SERVICE_ONBOARDING,
        notes="deployment default",
    ),
    CatalogEntry(
        key="telemetry.influx_token",
        value_type="string",
        lowest_level="deployment",
        secret=True,
        write_restricted=SERVICE_ONBOARDING,
        notes="delivered to Aggregators post-connect only (spec 16.4)",
    ),
    CatalogEntry(
        key="telemetry.influx_database",
        value_type="string",
        lowest_level="deployment",
        default="recordings",
        write_restricted=SERVICE_ONBOARDING,
    ),
    CatalogEntry(
        key="telemetry.prometheus_url",
        value_type="string",
        lowest_level="deployment",
        write_restricted=SERVICE_ONBOARDING,
        notes="read URL the platform queries; deployment default",
    ),
    CatalogEntry(
        key="telemetry.prom_remote_write_url",
        value_type="string",
        lowest_level="deployment",
        write_restricted=SERVICE_ONBOARDING,
        notes="receiver endpoint Aggregator agents push to; deployment default",
    ),
    CatalogEntry(
        key="telemetry.prom_remote_write_user",
        value_type="string",
        lowest_level="deployment",
        write_restricted=SERVICE_ONBOARDING,
    ),
    CatalogEntry(
        key="telemetry.prom_remote_write_password",
        value_type="string",
        lowest_level="deployment",
        secret=True,
        write_restricted=SERVICE_ONBOARDING,
        notes="delivered to Aggregators post-connect only (spec 16.4)",
    ),
    CatalogEntry(
        key="telemetry.grafana_url",
        value_type="string",
        lowest_level="deployment",
        write_restricted=SERVICE_ONBOARDING,
        notes="deployment default",
    ),
    CatalogEntry(
        key="services.credentials_generation",
        value_type="int",
        lowest_level="deployment",
        default=0,
        write_restricted=SERVICE_ONBOARDING,
        notes=(
            "bumped by every stack rotation (E5.11); the one non-secret signal "
            "that a deployment's service credentials changed"
        ),
    ),
)

CATALOG_BY_KEY: dict[str, CatalogEntry] = {entry.key: entry for entry in CATALOG}


def seed_catalog(connection: Connection) -> None:
    """Idempotent, convergent seed: upsert every constant row, then delete
    rows the constant no longer names. Called by the E2.1 migration and by
    every future catalog sync migration - because both halves converge, a
    from-scratch history replay and an in-place upgrade produce identical
    tables even after later constant edits (the import-app-code-in-a-
    migration hazard, neutralized)."""
    table = sa.table(
        "settings_catalog",
        sa.column("key", sa.String(100)),
        sa.column("value_type", sa.String(16)),
        sa.column("enum_values", JSONB()),
        sa.column("min_value", sa.Float()),
        sa.column("max_value", sa.Float()),
        sa.column("default_value", JSONB()),
        sa.column("lowest_level", sa.String(16)),
        sa.column("secret", sa.Boolean()),
        sa.column("resolution", sa.String(16)),
        sa.column("write_restricted", sa.String(30)),
        sa.column("notes", sa.String(500)),
        sa.column("version", sa.Integer()),
    )
    rows = [
        {
            "key": entry.key,
            "value_type": entry.value_type,
            "enum_values": list(entry.enum_values) if entry.enum_values is not None else None,
            "min_value": entry.min_value,
            "max_value": entry.max_value,
            "default_value": entry.default,
            "lowest_level": entry.lowest_level,
            "secret": entry.secret,
            "resolution": entry.resolution,
            "write_restricted": entry.write_restricted,
            "notes": entry.notes,
            "version": CATALOG_VERSION,
        }
        for entry in CATALOG
    ]
    statement = pg_insert(table).values(rows)
    updates = {name: statement.excluded[name] for name in rows[0] if name != "key"}
    connection.execute(statement.on_conflict_do_update(index_elements=["key"], set_=updates))
    connection.execute(table.delete().where(table.c.key.notin_([entry.key for entry in CATALOG])))
