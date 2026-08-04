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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


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
        Index("ix_deployment_tags", "tags", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
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
    reason: Mapped[str] = mapped_column(String(40))  # name_conflict | mac_conflict
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


# --- E2.1 settings catalog (spec 5.3; DECISIONS D47-D49) --------------------


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
