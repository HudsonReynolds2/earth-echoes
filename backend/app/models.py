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
    true,
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

#: The five deployment services of spec 16.2, in the order that table lists
#: them (task E5.1, phase-5 fixed choice 1). `deployment_service.service_key`
#: is CHECK-constrained to exactly this set: E3 wrote only `mqtt` because the
#: control plane cannot exist without broker coordinates, and E5 widens the
#: vocabulary rather than starting a second table. A sixth key needs a spec
#: change first - `tests/test_services_model.py` pins this tuple against a
#: hand transcription of the spec table, the way test_settings_catalog.py
#: pins CATALOG against spec 5.3.
SERVICE_KEYS: tuple[str, ...] = ("mqtt", "influx", "prometheus", "grafana", "s3")

#: Per-service verification status (spec 16.2's "per-service status"), a
#: property of ONE service connection and stored on its own row. E5.5 owns
#: every transition; E5.1 only creates the column and its `untested` default.
SERVICE_STATUS_VOCAB: tuple[str, ...] = ("untested", "verified", "failed")

#: The rolled-up `deployment.services_status` of spec 16.5 - a DIFFERENT
#: vocabulary from the per-service one, deliberately, because "this
#: deployment is degraded" is not a statement any single service can make.
#: E5.5's `app/services/status.py::roll_up` is its only writer.
SERVICES_STATUS_VOCAB: tuple[str, ...] = (
    "unconfigured",
    "pending_verification",
    "verified",
    "degraded",
)


#: The lifecycle of a per-device broker login (E5.6). `revoke_pending` is the
#: one that needed a decision rather than following from the domain: deleting
#: an Aggregator while its broker is unreachable must neither block the
#: operator nor leave a decommissioned Pi holding live credentials, so the
#: delete proceeds and the revocation is retried. Owner's call, 2026-08-12;
#: DECISIONS D121, project-changes #27.
BROKER_CREDENTIAL_STATES: tuple[str, ...] = ("minted", "revoke_pending", "revoked")


def _in_vocab(column: str, vocab: tuple[str, ...]) -> str:
    """A SQL `IN` predicate rendered from a vocabulary tuple, so the CHECK and
    the constant can never disagree."""
    return f"{column} IN ({', '.join(repr(value) for value in vocab)})"


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
        CheckConstraint(
            _in_vocab("services_status", SERVICES_STATUS_VOCAB), name="services_status_vocab"
        ),
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
    #: E5.1: the spec 16.5 rollup over this deployment's `deployment_service`
    #: rows (phase-5 fixed choice 2). DENORMALIZED deliberately - E6.4's map
    #: rollup and E7.4's Owner fan-out both read it once per deployment inside
    #: fan-outs that are already cross-deployment, and a join per deployment to
    #: answer "is this stack healthy" is the cost being avoided. The
    #: correctness risk of denormalizing is answered by making E5.5's
    #: `app/services/status.py::roll_up` the ONLY writer and asserting the
    #: invariant across the suite. E5.1 creates the column and its default and
    #: writes nothing.
    services_status: Mapped[str] = mapped_column(
        String(30), default="unconfigured", server_default=text("'unconfigured'")
    )
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
    E3.1, widened by E5.1; spec 7.1, 16.2).

    **E5 EXTENDS THIS TABLE; it does not fork it** (phase-5 fixed choice 1).
    E3 defined the row shape and populated exactly one service_key, 'mqtt',
    because the control plane cannot exist without broker coordinates. E5.1
    widens `service_key` to the five spec 16.2 services and adds the columns
    the other four need, rather than starting a second table.

    **The six MQTT-shaped columns stay exactly where they are.** Moving them
    into `config` would rewrite `load_broker_coordinates`,
    `devbroker.register_services` and the `port_range` constraint for no
    benefit, and `load_broker_coordinates` is the function every deployment's
    control plane depends on. They become CONDITIONALLY REQUIRED instead: the
    `mqtt_coordinates_required` CHECK makes `host`, `port`, `username` and
    `password_secret_name` NOT NULL for an `mqtt` row and optional for every
    other, so a Grafana row is not four meaningless empty strings.

    **`config` and `secret_names` carry everything else.** Fifteen nullable
    columns whose validity is a function of `service_key` is a schema that
    documents nothing and constrains nothing, and a CHECK cannot validate a
    URL anyway - so the heterogeneous per-service fields are typed at the
    WRITE BOUNDARY instead, in one Pydantic model per service (E5.2, rule
    R2). `secret_names` maps a field name to its SecretStore name and
    **never to a value**, exactly as `password_secret_name` does for the
    broker; it is the same D51 discipline in map form.

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
        CheckConstraint(_in_vocab("service_key", SERVICE_KEYS), name="service_key_vocab"),
        CheckConstraint("port > 0 AND port < 65536", name="port_range"),
        # Conditional requirement, phase-5 fixed choice 1: the broker row
        # still cannot exist without the four fields E3 dials it with, and
        # the DATABASE is what says so - not a Python guard a later writer
        # could route around.
        CheckConstraint(
            "service_key <> 'mqtt' OR ("
            "host IS NOT NULL AND port IS NOT NULL "
            "AND username IS NOT NULL AND password_secret_name IS NOT NULL)",
            name="mqtt_coordinates_required",
        ),
        CheckConstraint(_in_vocab("status", SERVICE_STATUS_VOCAB), name="status_vocab"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deployment.id"), index=True)
    service_key: Mapped[str] = mapped_column(String(40))
    host: Mapped[str | None] = mapped_column(String(255), default=None)
    port: Mapped[int | None] = mapped_column(default=None)
    tls_enabled: Mapped[bool] = mapped_column(default=True)
    ca_cert_pem: Mapped[str | None] = mapped_column(Text, default=None)
    username: Mapped[str | None] = mapped_column(String(200), default=None)
    password_secret_name: Mapped[str | None] = mapped_column(String(200), default=None)
    #: The heterogeneous, non-secret per-service fields (Influx database name,
    #: S3 bucket and region, Grafana base URL, ...). Typed at the write
    #: boundary by E5.2's per-service Pydantic models, never here.
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    #: field name -> SecretStore name. **Never a value** (rule R2); the same
    #: rule `password_secret_name` follows, in map form.
    secret_names: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Spec 16.2's per-service status block. E5.1 creates these columns;
    #: **E5.5's `apply_test_results` is what writes them**, and E5.3's testers
    #: are what produce the evidence. `consecutive_failures` is state rather
    #: than a heuristic because spec 16.5 demotes "on repeated failure", and a
    #: counter incremented on fail and zeroed on pass is the smallest thing
    #: that makes "repeated" true without a history table.
    status: Mapped[str] = mapped_column(
        String(20), default="untested", server_default=text("'untested'")
    )
    #: E5.5: whether this service has to reach `verified` for the DEPLOYMENT
    #: to. True for everything by default; set False when a tester answers
    #: `not_required` - spec 16.2 makes object storage conditionally required,
    #: and a deployment with raw-audio upload off must still be able to verify.
    #: **A stored column rather than a parameter to `roll_up`, because
    #: `deployment.services_status` has to be reproducible from these rows
    #: alone.** It was a parameter first, and the suite-wide invariant
    #: assertion caught it on the first run: a rollup depending on a fact known
    #: only during a test run cannot be recomputed afterwards, which is exactly
    #: the divergence denormalizing the column risks (D117).
    required: Mapped[bool] = mapped_column(default=True, server_default=true())
    status_reason: Mapped[str | None] = mapped_column(Text, default=None)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    consecutive_failures: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    #: The last structured `TestResult` (E5.3), for the S5 wizard's remedy
    #: text. Redaction is the tester's job: nothing here may name a credential.
    last_test_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    deployment: Mapped[Deployment] = relationship()


class BrokerCredential(Base):
    """One per-device broker login the platform minted (task E5.6; spec 16.4).

    Fixed choice 4 makes Mosquitto's dynamic security plugin required, so this
    row is the platform's record of a client that exists **on a broker** — a
    thing outside this database that has to be cleaned up deliberately. That
    single fact decides most of the shape below.

    **`aggregator_uuid` is a plain string and deliberately NOT a foreign key
    to `aggregator`.** The row has to OUTLIVE the device: deleting a Pi is
    exactly when its credential must be destroyed, and a cascade would delete
    the platform's only record of a login that is still live on the broker.
    Spec 4.2 also makes `aggregator_uuid` the identifier the device itself
    carries, which is what the broker knows it by.

    **Three states, not the two the phase document pencilled** (project-changes
    #27). `minted` and `revoked` are the steady ones; `revoke_pending` exists
    because a broker that is unreachable must not be able to block an operator
    from deleting inventory, while a decommissioned Pi must still end up with a
    dead credential. The owner chose that trade on 2026-08-12. The sweep in
    `app/services/credentials.py::drain_pending_revocations` is what closes it,
    and `revoked_at` stays NULL until the broker has actually confirmed.

    Credentials never live here: `password_secret_name` names a SecretStore
    entry and the plaintext moves only through `app.secrets.SecretStore`
    (rule R2), exactly as `deployment_service.password_secret_name` does.
    """

    __tablename__ = "broker_credential"
    __table_args__ = (
        # One live credential per device. A rotation REPLACES this row's
        # password rather than accumulating history: two `minted` rows for one
        # `aggregator_uuid` would be two logins on the broker and no way to
        # tell which the device holds.
        UniqueConstraint("deployment_id", "aggregator_uuid"),
        CheckConstraint(
            _in_vocab("state", BROKER_CREDENTIAL_STATES),
            name="state_vocab",
        ),
        # A row is `revoked` exactly when the broker confirmed it. Making the
        # database say so keeps `revoked_at` from becoming a field that means
        # "we asked" on some rows and "it is gone" on others.
        CheckConstraint(
            "(state = 'revoked') = (revoked_at IS NOT NULL)",
            name="revoked_at_matches_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deployment.id"), index=True)
    aggregator_uuid: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str] = mapped_column(String(200))
    password_secret_name: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(
        String(20), default="minted", server_default=text("'minted'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

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

    **E3.9 EXTENDS THIS TABLE** with the spec 6.5 Listener liveness block,
    which arrives inside a report and so belongs here. E3.5 deliberately
    stores none of it, so that no column is a half-implementation of a task
    that has not run.

    **E3.8 did NOT extend it, though E3.5 anticipated it would** (D88). LWT
    online state went to `aggregator_status` instead: `reported_at`,
    `checksum` and `config` are NOT NULL and a device publishes `online`
    before it has ever reported a config, so a status-only row would have
    required making three of them nullable — dissolving the invariant that a
    row here IS a report. An `offline` LWT is also published by the BROKER on
    the device's behalf, which is precisely the state the device did not send.

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
        # E3.9. The spec 6.5 vocabulary, and the two shape rules the wire
        # contract already enforces (`contracts.mqtt.ListenerLiveness`),
        # repeated here because a constraint in Pydantic protects the boundary
        # and a constraint in Postgres protects the table. A `sleeping` row
        # with no wake time cannot be told from silence, and a wake time on a
        # streaming row is a stale value something will eventually act on.
        CheckConstraint(
            "liveness_state IS NULL OR liveness_state IN ('streaming','sleeping','offline')",
            name="liveness_state_vocab",
        ),
        CheckConstraint(
            "(liveness_state = 'sleeping') = (expected_wake_at IS NOT NULL)",
            name="wake_time_belongs_to_sleeping",
        ),
        # Aggregators have no spec 6.5 liveness at all — theirs is the LWT
        # verdict in `aggregator_status` (E3.8). A liveness value on an
        # aggregator row would be a second, quieter answer to a question that
        # already has an authoritative one.
        CheckConstraint(
            "entity_type = 'listener' OR liveness_state IS NULL",
            name="liveness_is_listener_only",
        ),
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

    # -- spec 6.5 Listener liveness (E3.9), NULL on every aggregator row -----
    #
    # The AGGREGATOR tracks all of this and the platform only records it. The
    # Listener declares its own wake time over the local HaLow link, the
    # Aggregator trusts that declaration rather than recomputing the schedule
    # (the Listener's own clock is what governs when it actually wakes), and
    # the platform is a further step removed still: it must never compute a
    # wake window, apply a grace period, or decide on its own that a Listener
    # has missed one. `listener.wake_grace_seconds` is a DEVICE setting that
    # rides the config down; nothing up here reads it.
    #: `streaming` | `sleeping` | `offline`, verbatim from the report. NULL
    #: means no Listener report has arrived yet, which is not the same as
    #: offline — see `liveness.py`.
    liveness_state: Mapped[str | None] = mapped_column(String(20), default=None)
    #: Last audio the Aggregator saw. Diagnostic; drives no verdict.
    last_audio_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: Present exactly while sleeping (spec 7.3), enforced both at the wire
    #: boundary and by `wake_time_belongs_to_sleeping` above. The moment the
    #: Listener PROMISED to be back, not a deadline the platform enforces.
    expected_wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: When `liveness_state` last actually changed, which is what "offline
    #: since" reads. A re-report of the same state must not move it, the same
    #: rule `aggregator_status.changed_at` follows for the LWT verdict.
    liveness_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
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


class AggregatorStatus(Base):
    """An Aggregator's live online verdict, driven by MQTT (task E3.8; spec 9.3, 7.2, 7.3).

    Spec 9.3 makes MQTT the AUTHORITATIVE real-time liveness signal for an
    Aggregator, and deliberately not Prometheus: the remote-write agent
    buffers to a write-ahead log and backfills on reconnect (spec 10.4), so
    central Prometheus lags real time by design. This row is what the status
    dot reads.

    **Not a column on `device_state`, though E3.5's docstring anticipated
    one.** Three reasons, and the third is decisive. `device_state` is defined
    as "the last state the device REPORTED" and its `reported_at`, `checksum`
    and `config` are NOT NULL, but a device publishes `online` before it has
    ever reported a config — so a status-only row could not be written without
    making three of E3.5's columns nullable and dissolving the invariant that
    a `device_state` row IS a report. LWT is also Aggregator-only (Listeners
    hold no MQTT session, spec 6.4/9.3, and E3.9 stores their liveness on the
    report where it belongs). And an `offline` LWT is published by the BROKER
    on the device's behalf: it is precisely the state the device did not send.

    **`at` is NOT an ordering key, and this is the trap the table exists to
    avoid.** A device composes its will at CONNECT time and the broker holds
    those exact bytes until the session dies, so the `at` on an `offline`
    message is older than every `online` heartbeat that followed it — often by
    hours. Ordering status by the payload clock the way spec 7.4 orders
    reports would reject every LWT as stale and leave dead devices reading
    online forever. Receipt order is the truth here: one broker, QoS 1, one
    ordered session per device, and a retained replay always carries the
    CURRENT value. `declared_at` is stored because the device said it, and
    read by nothing that decides anything.
    """

    __tablename__ = "aggregator_status"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    #: Real FK with a cascade, unlike `device_state.entity_id`: there is one
    #: target table here rather than two, and this is CURRENT state, not
    #: evidence — it dies with its device instead of outliving it. The
    #: `device_event` rows keep the history.
    aggregator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("aggregator.id", ondelete="CASCADE"), unique=True
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deployment.id"), index=True)
    #: The spec 9.3 verdict. No `unknown` third state: a device that has never
    #: spoken has no row at all, which is a different question from one the
    #: platform has heard call itself offline.
    online: Mapped[bool] = mapped_column()
    #: The payload's own `at`. Stored as sent, never compared — see the class
    #: docstring. On an LWT this is the moment the device CONNECTED.
    declared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: When `online` last actually changed value, which is what "offline since"
    #: means on screen. A retained replay of the same state must not move it.
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Platform receipt, and the real ordering key.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReconciliationEvent(Base):
    """One spec 6.2 transition, as timeline evidence (task E3.11; spec 6.3).

    Spec 6.3 asks for "every transition with a timestamp, the actor (user or
    system), the before/after effective config diff, and any device-supplied
    detail". This is that row, and it is written by
    `revision_state.transition` and by nothing else — the same argument that
    put `published_at` there (D84). Every state change in the system passes
    through that one function, so recording the timeline inside it makes the
    timeline complete BY CONSTRUCTION rather than by every call site
    remembering to log.

    **Evidence, not current state**, so it is append-only and un-FK'd on the
    D33 precedent: a transition must survive the revision being pruned and the
    device being decommissioned. It is what the E3.11 timeline renders after
    the thing it describes is gone.

    **Two fields, because they have two different provenances.** `diff` is the
    PLATFORM's side — how this revision's config differs from the one before
    it, taken from revision snapshots, which hold secret markers rather than
    plaintext (spec 5.4) and so are safe to store whole. `detail` is the
    DEVICE's side, or the worker's: differing key NAMES on a mismatch, the
    Aggregator's own error text. Values from a device are of unknown
    provenance and never land in `diff`.
    """

    __tablename__ = "reconciliation_event"
    __table_args__ = (
        # The timeline query: one device, newest first.
        Index("ix_reconciliation_event_timeline", "target_type", "target_id", "at"),
        Index("ix_reconciliation_event_scope", "deployment_id", "at"),
        CheckConstraint("target_type IN ('aggregator','listener')", name="target_type_vocab"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    #: Un-FK'd (D33): history outlives the revision it describes.
    revision_id: Mapped[uuid.UUID] = mapped_column(index=True)
    #: The `config_revision` convention exactly — aggregators by PLATFORM
    #: UUID, listeners by MAC (D75) — so a device's timeline is one query.
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(100))
    #: For the per-deployment audit view (spec 6.3). Un-FK'd like the rest.
    deployment_id: Mapped[uuid.UUID] = mapped_column()
    from_state: Mapped[str] = mapped_column(String(20))
    to_state: Mapped[str] = mapped_column(String(20))
    trigger: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    #: NULL means the system did it — a timeout, a device report, a drift
    #: sweep. The same convention `audit_log` uses, so the two surfaces agree
    #: on what "no actor" looks like.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    #: `{key: {"before": ..., "after": ...}}` against the previous revision for
    #: this device, from SNAPSHOTS only. Present on entry to `pending`, where
    #: the config change actually happens; None on the other edges, which move
    #: state without changing what was asked for.
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    #: Device- or worker-supplied. Key NAMES on a mismatch, never values.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
