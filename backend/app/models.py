"""Persistent models (task E0.6 onward). Everything inherits app.db.Base so
the E0.2 naming convention names every constraint."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

#: The phase-3 fixed choice: a published revision that has not been reported
#: within this many seconds is failed as a timeout (spec 6.4 item 4). Per
#: deployment, overridable on the row; this is the value a new deployment gets
#: and the value the worker falls back to when a revision outlives the
#: deployment row it names (`config_revision.deployment_id` is un-FK'd, D33).
DEFAULT_PENDING_TIMEOUT_SECONDS = 300


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    """A local account (spec 12.2). Role assignment arrives with E0.7."""

    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(default=True)
    # E0.10: the TOTP secret itself lives in SecretStore under totp:{id};
    # this flag only records that enrollment was confirmed.
    totp_enabled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user")
    role_assignments: Mapped[list["RoleAssignment"]] = relationship(
        back_populates="user", lazy="selectin"
    )


class RoleAssignment(Base):
    """Deployment-scoped role grant (task E0.7; spec 12.3).

    deployment_id now carries the real foreign key E1.1 was designed to add
    (phase-0 E0.7 fixed this seam explicitly; DECISIONS D33); NULL still means
    the grant is organization-wide. The FK is plain NO ACTION — DELETE
    /deployments treats grants as blocking children (409) before the database
    ever sees the delete. Role values come from app.auth.rbac.Role; stored as
    strings so adding a role never needs an enum migration.
    """

    __tablename__ = "role_assignment"
    __table_args__ = (UniqueConstraint("user_id", "role", "deployment_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"), index=True)
    role: Mapped[str] = mapped_column(String(40))
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deployment.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="role_assignments")


class AuditLog(Base):
    """Immutable audit row (task E0.8; spec 14.1, 13; addendum PHASE0-4-02).

    Application code has NO update or delete path for this table, and the
    migration revokes UPDATE/DELETE at the database layer (decision D3).
    entity_type/entity_id are deliberately untyped strings so later phases
    log hierarchy entities (including MAC-keyed Listeners) without schema
    churn; scope holds a deployment id, NULL meaning organization-wide
    (spec 12.1: no denormalized tenant columns).
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True, default=None
    )
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(100))
    scope: Mapped[uuid.UUID | None] = mapped_column(index=True, default=None)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    request_id: Mapped[str] = mapped_column(String(64), default="-")


class Secret(Base):
    """Envelope-encrypted secret at rest (task E0.11; spec 12.4).

    Only ciphertext ever touches this table: the per-secret DEK encrypts the
    value, the platform KEK (EOE_KEK) wraps the DEK, and kek_fingerprint
    records which KEK did the wrapping so rotation can find its rows. All
    access goes through app.secrets.SecretStore; nothing else reads or writes
    here (rule R2).
    """

    __tablename__ = "secret"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    dek_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    kek_fingerprint: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserSession(Base):
    """DB-backed session row (decision D1): the cookie carries a signed opaque
    id pointing here, so logout and admin revocation are immediate."""

    __tablename__ = "session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    user_agent: Mapped[str] = mapped_column(String(400), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")

    user: Mapped[User] = relationship(back_populates="sessions")

    def is_valid(self, at: datetime | None = None) -> bool:
        moment = at if at is not None else utcnow()
        return self.revoked_at is None and moment < self.expires_at


# --- E1.1 hierarchy (spec 4.1, 4.2; DECISIONS D30-D32) ---------------------
# Table names are singular, matching E0's convention over the phase doc's
# plural spelling (D30); the URL collections stay plural per spec 13, the
# same split `user` / `/users` already established.


class Organization(Base):
    """Hierarchy root (spec 4.1). v1 runs a single Organization (spec 12.1);
    the API clamps POST when one exists (D34). Access scoping flows through
    the FK chain by join — no denormalized tenant id on any table."""

    __tablename__ = "organization"
    __table_args__ = (Index("ix_organization_tags", "tags", postgresql_using="gin"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    deployments: Mapped[list["Deployment"]] = relationship(back_populates="organization")


class Deployment(Base):
    """One telemetry stack plus one MQTT broker (spec 4.1). slug keys the
    MQTT topic namespace ({dep}, spec 7.2): lowercase URL-safe, globally
    unique, format-checked here, and frozen by the API once the deployment
    has any pod (D36) — E3 depends on it never changing under live topics."""

    __tablename__ = "deployment"
    __table_args__ = (
        UniqueConstraint("organization_id", "name"),
        CheckConstraint("slug ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'", name="slug_format"),
        CheckConstraint("pending_timeout_seconds > 0", name="pending_timeout_positive"),
        Index("ix_deployment_tags", "tags", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    #: E3.7: the spec 6.4 item 4 window, in seconds, after which a `pending`
    #: revision this deployment owns is failed as a timeout. A PLATFORM
    #: setting, not a device setting (phase-3 fixed choice), so it lives on the
    #: deployment row rather than in the settings catalog - no device ever
    #: reads it and it must never reach a desired topic.
    pending_timeout_seconds: Mapped[int] = mapped_column(
        default=DEFAULT_PENDING_TIMEOUT_SECONDS, server_default=str(DEFAULT_PENDING_TIMEOUT_SECONDS)
    )
    #: E3.7: stored, default off, and deliberately INERT. Spec 6.2 names an
    #: "auto-reconcile policy" as a second driver of drifted -> pending, and
    #: spec 17 item 3 has not decided what that policy should be; until it
    #: does, drift is repaired by an operator re-publishing. Nothing in the
    #: worker reads this to decide an action - the only code that may read it
    #: reports it.
    auto_reconcile: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="deployments")
    pods: Mapped[list["Pod"]] = relationship(back_populates="deployment")


class Pod(Base):
    """One arm of the star around a HaLow router: one Aggregator plus its
    Listeners (spec 4.1). Name unique within the parent deployment."""

    __tablename__ = "pod"
    __table_args__ = (
        UniqueConstraint("deployment_id", "name"),
        Index("ix_pod_tags", "tags", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deployment.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    deployment: Mapped[Deployment] = relationship(back_populates="pods")
    aggregator: Mapped["Aggregator | None"] = relationship(back_populates="pod")


class Aggregator(Base):
    """Pi/balenaOS gateway, exactly one per Pod — enforced by the UNIQUE on
    pod_id, not by application discipline (spec 13; task E1.3). Three
    identity columns, never conflated (spec 4.2): id is the platform UUID,
    aggregator_uuid is the first-class join key unifying the three data
    planes (unique; single-org v1 makes global uniqueness the within-org
    rule, D32), balena_uuid is the Balena cross-reference."""

    __tablename__ = "aggregator"
    __table_args__ = (Index("ix_aggregator_tags", "tags", postgresql_using="gin"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pod_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pod.id"), unique=True)
    aggregator_uuid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    balena_uuid: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str | None] = mapped_column(String(200), default=None)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    pod: Mapped[Pod] = relationship(back_populates="aggregator")
    listeners: Mapped[list["Listener"]] = relationship(back_populates="aggregator")


class Listener(Base):
    """ESP32-S3 listener keyed by its immutable MAC (spec 4.2; D31) —
    normalized to uppercase colon-separated form at the API boundary and
    format-checked here. deployment_id is a set-once denormalized stamp
    (D32): parent fields are create-only across the whole hierarchy, so the
    stamp cannot drift, and it makes the spec 4.3 name-unique-within-
    deployment rule a real database constraint. It is not a tenant column
    (spec 12.1 - no organization_id is stamped anywhere)."""

    __tablename__ = "listener"
    __table_args__ = (
        UniqueConstraint("deployment_id", "name"),
        CheckConstraint("mac ~ '^[0-9A-F]{2}(:[0-9A-F]{2}){5}$'", name="mac_format"),
        Index("ix_listener_tags", "tags", postgresql_using="gin"),
    )

    mac: Mapped[str] = mapped_column(String(17), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    aggregator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("aggregator.id"), index=True)
    deployment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deployment.id"), index=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, default=None)
    gps_lon: Mapped[float | None] = mapped_column(Float, default=None)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    aggregator: Mapped[Aggregator] = relationship(back_populates="listeners")


# --- E1.5 report-time identity (spec 4.3 items 2-3; DECISIONS D37) ----------


class QuarantinedReport(Base):
    """Spec 4.3 item 2: a conflicting reported identity lands here instead of
    touching inventory. Deliberately NO foreign key to listener - the row must
    survive listener deletion and must be able to describe devices that never
    existed in inventory. Append-only evidence: every conflicting report adds
    a row (D37); alerts dedupe, quarantine does not."""

    __tablename__ = "quarantined_report"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    mac: Mapped[str] = mapped_column(String(17), index=True)
    reported_name: Mapped[str | None] = mapped_column(String(200), default=None)
    aggregator_uuid: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    # name_conflict | mac_conflict (E1.5) | unknown_mac (E3.5, D76: a Listener
    # reporting before anyone entered it in inventory - not a conflict, so no
    # alert, but evidence an operator should be able to find and adopt).
    reason: Mapped[str] = mapped_column(String(40))
    report: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InventoryAlert(Base):
    """E1-owned alert rows (`duplicate_identity`, `provisioning_required`);
    E7 unifies alert surfacing later. Open alerts dedupe per (type, entity)
    via the partial unique index below - a second identical conflict returns
    the existing open alert instead of stacking rows (D37). deployment_id is
    scope for filtering, deliberately un-FK'd: an alert may outlive its
    deployment, like audit rows (D33)."""

    __tablename__ = "inventory_alert"
    __table_args__ = (
        Index(
            "uq_inventory_alert_open",
            "alert_type",
            "entity_type",
            "entity_key",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    alert_type: Mapped[str] = mapped_column(String(40))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_key: Mapped[str] = mapped_column(String(100))
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(index=True, default=None)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


# --- E2 configuration model (spec 5; DECISIONS D47-D56) ---------------------


class SettingsCatalog(Base):
    """Versioned settings schema, one row per spec-5.3 key (task E2.1).
    Seeded in-migration from app.config.catalog.CATALOG, the single source;
    a gate test holds table and constant equal. The catalog is data, not
    hard-coded UI (spec 5.3): the frontend renders editors from these rows,
    so a new key ships with no frontend change.

    default_value is JSONB (named to dodge the SQL DEFAULT keyword; the API
    field is "default"); SQL NULL means no default and the notes say which
    kind. resolution 'inventory' marks keys that resolve from listener
    columns and reject overrides (location.* mandated by E1's contract,
    identity.* the same character - D49). write_restricted
    'service_onboarding' marks keys the E5 flow writes (spec 5.3 closing
    paragraph); the generic override PUT rejects them until then (D48)."""

    __tablename__ = "settings_catalog"
    __table_args__ = (
        CheckConstraint(
            "value_type IN ('int','float','bool','string','object')", name="value_type_vocab"
        ),
        CheckConstraint(
            "lowest_level IN ('listener','aggregator','pod','deployment','organization','any')",
            name="lowest_level_vocab",
        ),
        CheckConstraint("resolution IN ('override','inventory')", name="resolution_vocab"),
    )

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_type: Mapped[str] = mapped_column(String(16))
    enum_values: Mapped[list[Any] | None] = mapped_column(JSONB, default=None)
    min_value: Mapped[float | None] = mapped_column(Float, default=None)
    max_value: Mapped[float | None] = mapped_column(Float, default=None)
    default_value: Mapped[Any] = mapped_column(JSONB, nullable=True, default=None)
    lowest_level: Mapped[str] = mapped_column(String(16))
    secret: Mapped[bool] = mapped_column(default=False)
    resolution: Mapped[str] = mapped_column(String(16), default="override")
    write_restricted: Mapped[str | None] = mapped_column(String(30), default=None)
    notes: Mapped[str] = mapped_column(String(500), default="")
    version: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConfigRevision(Base):
    """Immutable desired-config snapshot (task E2.6; spec 6.1, 6.2; D55).
    PER-DEVICE only: the spec 7.2 desired topics address Aggregators and
    Listeners, so those are the only target types - pods and organizations
    never carry revisions. target_id and deployment_id are deliberately
    un-FK'd (the D33 immutable-evidence precedent): revision history outlives
    the devices and deployments it describes and must never block deletion.

    snapshot holds the device's full effective config as flat dotted keys
    with secret MARKERS in place, never plaintext - secrets don't transit
    desired topics (spec 5.4, 8), so the snapshot is the publishable payload
    body and checksum (the D52 recipe) matches device echoes by
    construction. Listener snapshots exclude the write-restricted service
    keys (spec 5.4); aggregator snapshots include them. state is a string
    from the spec 6.2 vocabulary; E2 writes 'draft' ONLY - every other state
    belongs to E3's machine, gated by EOE_PUBLISH_ENABLED."""

    __tablename__ = "config_revision"
    __table_args__ = (
        CheckConstraint("target_type IN ('aggregator','listener')", name="target_type_vocab"),
        Index("ix_config_revision_target", "target_type", "target_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(100))
    deployment_id: Mapped[uuid.UUID] = mapped_column(index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(default=1)
    checksum: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: E3.7: when this revision last ENTERED `pending`, written by
    #: `revision_state.transition` and by nothing else. The spec 6.4 item 4
    #: window is measured from here rather than from `created_at`, because a
    #: revision reaching `pending` a second time (an operator retrying a
    #: `failed` one, or re-publishing over drift) starts a fresh wait - from
    #: `created_at` it would time out the instant it was retried. In Postgres
    #: rather than in the worker so a restarted worker resumes the same
    #: windows it left (spec 14.3).
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )


class Selection(Base):
    """A saved device selection (task E2.5; spec 5.2, 13; D54). query holds
    the validated grammar document VERBATIM and is re-evaluated at every
    use, re-filtered through the caller's visible deployments - never a
    materialized id list, so membership tracks the fleet and the actor's
    grants. Spec 13 ships GET/POST only: no rename, no delete (recorded)."""

    __tablename__ = "selection"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    query: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EntityOverride(Base):
    """One sparse override map per hierarchy entity (task E2.2; spec 5.1;
    D50-D51). Singular table name per D30 (the phase doc spells it plural).

    entity_id is an untyped String - UUID string for four entity kinds, MAC
    for listeners - the audit_log precedent, deliberately un-FK'd because the
    five targets live in five tables; cleanup rides the E1 DELETE endpoints
    (delete_overrides_for, wired in E2.4). overrides holds flat dotted keys
    validated against the catalog on every write; a secret-flagged key's
    value is the marker {"$secret": "config:{entity_type}:{entity_id}:{key}"}
    and the plaintext lives in SecretStore under that name, never here."""

    __tablename__ = "entity_override"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id"),
        CheckConstraint(
            "entity_type IN ('organization','deployment','pod','aggregator','listener')",
            name="entity_type_vocab",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[str] = mapped_column(String(100))
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    catalog_version: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --- E3 control plane (spec 6, 7) ------------------------------------------


class DeploymentService(Base):
    """A deployment-local service the platform connects outbound to (task
    E3.1; spec 7.1, 16.2).

    **E5 OWNS EXTENDING THIS TABLE.** E3 defines the row shape and populates
    exactly one service_key, 'mqtt', because the control plane cannot exist
    without broker coordinates. The Influx / Grafana / Prometheus / S3 rows,
    the connection tests, and the verification status lifecycle (spec 16.5)
    are E5's; adding them means adding columns here, not a second table.

    Credentials never live in this row: `password_secret_name` names a
    SecretStore entry (`deployment:{deployment_id}:{service_key}_password`)
    and the plaintext moves only through app.secrets.SecretStore (rule R2).
    ca_cert_pem is deliberately NOT a secret - it is the public certificate
    the platform must trust to verify the broker's TLS identity, and storing
    the PEM rather than a path keeps a deployment's trust anchor portable
    across API replicas and container filesystems.
    """

    __tablename__ = "deployment_service"
    __table_args__ = (
        UniqueConstraint("deployment_id", "service_key"),
        CheckConstraint("service_key IN ('mqtt')", name="service_key_vocab"),
        CheckConstraint("port > 0 AND port < 65536", name="port_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deployment.id"), index=True)
    service_key: Mapped[str] = mapped_column(String(40))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column()
    tls_enabled: Mapped[bool] = mapped_column(default=True)
    ca_cert_pem: Mapped[str | None] = mapped_column(Text, default=None)
    username: Mapped[str] = mapped_column(String(200))
    password_secret_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    deployment: Mapped[Deployment] = relationship()


class DeviceState(Base):
    """The last state a device REPORTED (task E3.5; spec 6.1, 7.3, 7.4).

    Spec 6.1's other half: every device carries a desired configuration (the
    `config_revision` rows) and a reported configuration, "the last state the
    device sent". One row per device, replaced in place — this is current
    state, not evidence, so it is deleted with its device rather than
    outliving it (`delete_device_state_for`, the E2.4 override-cleanup
    precedent). The append-only record of what a device said over time is
    `device_event` and, for config outcomes, the revision transitions.

    **`reported_at` is what makes spec 7.4's ordering rule enforceable.** A
    report older than the stored one is a late redelivery describing a world
    that has already moved on, and applying it would drive a healthy device to
    `drifted` on the strength of stale news. Equal timestamps are NOT stale:
    a byte-identical replay runs the full comparison and reaches "already
    there", so idempotency comes from `applied_revision_id` plus checksum as
    spec 7.4 words it, rather than from a timestamp shortcut.

    **E3.8 AND E3.9 EXTEND THIS TABLE** (the `deployment_service` pattern):
    E3.8 adds the LWT-driven online state that spec 9.3 makes authoritative
    for an Aggregator's live verdict, E3.9 the spec 6.5 Listener liveness
    block. E3.5 deliberately stores neither, so that no column here is a
    half-implementation of a task that has not run.

    `entity_type`/`entity_id` follow the `config_revision` convention exactly
    — aggregators by PLATFORM UUID (`aggregator.id`), listeners by MAC (D75)
    — so a device's revisions and its reported state join without translating
    between spec 4.2's three identifiers. `entity_id` is deliberately un-FK'd
    for the `entity_override` reason: two target kinds live in two tables.
    """

    __tablename__ = "device_state"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id"),
        CheckConstraint("entity_type IN ('aggregator','listener')", name="entity_type_vocab"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[str] = mapped_column(String(100))
    deployment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deployment.id"), index=True)
    #: The device's own clock, from the payload — the spec 7.4 ordering key.
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: NULL while a device has applied nothing yet; un-FK'd like every other
    #: reference to a revision, so history can be pruned without erasing state.
    applied_revision_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    checksum: Mapped[str] = mapped_column(String(80))
    #: The reported config verbatim, secret MARKERS included (never plaintext:
    #: they were markers on the desired topic too, spec 5.4). E3.7 re-compares
    #: this against the desired snapshot to detect drift without a new report.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    #: Spec 7.3's coarse hint, stored as sent and NOT charted — Prometheus is
    #: the authoritative metrics source (spec 10.1) and two sources of truth
    #: for one number is exactly what that split exists to prevent.
    health: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    #: When the PLATFORM took delivery. Differs from `reported_at` by the
    #: broker's queueing, which is the gap that makes a late report late.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeviceEvent(Base):
    """One spec 7.3 event from an Aggregator's event topic (task E3.5).

    Immutable evidence, so `deployment_id`, `aggregator_uuid` and
    `listener_mac` are deliberately un-FK'd on the D33 precedent: an event is
    the record that something happened, and it must survive the device it
    describes being decommissioned. E7 owns alerts; E3 persists these and
    E3.11 renders them on the device timeline, which is why `code` stays
    machine-readable and `detail` stays human-readable.

    **Redelivery is a no-op, not a second row.** QoS 1 is at-least-once and an
    event carries no device-supplied id, so identity is (emitter, instant,
    code) — two distinct events with one code from one device in the same
    instant are indistinguishable on the wire anyway, and a duplicated
    timeline entry is a lie about how often something happened. The unique
    index backstops the consumer's check against races, the way
    `inventory_alert`'s partial index backstops E1.5's. `NULLS NOT DISTINCT`
    is load-bearing: without it every Aggregator-level event (`listener_mac`
    NULL) would be unique to Postgres and dedupe only Listener events.
    """

    __tablename__ = "device_event"
    __table_args__ = (
        Index(
            "uq_device_event_delivery",
            "deployment_id",
            "aggregator_uuid",
            "listener_mac",
            "at",
            "code",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_device_event_timeline", "aggregator_uuid", "at"),
        CheckConstraint("level IN ('debug','info','warn','error')", name="level_vocab"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(index=True)
    #: The EMITTER, which is the identity the broker ACL authenticated — the
    #: `{agg}` segment of the topic it arrived on, never a payload field.
    aggregator_uuid: Mapped[str] = mapped_column(String(64))
    #: Set when the event is about one Listener; both of the spec 7.3 named
    #: codes are. Not validated against inventory: the emitter is what was
    #: authenticated, and refusing to record an event about an unknown MAC
    #: would discard the report that an unknown Listener exists.
    listener_mac: Mapped[str | None] = mapped_column(String(17), default=None, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(10))
    code: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
