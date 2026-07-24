"""Persistent models (task E0.6 onward). Everything inherits app.db.Base so
the E0.2 naming convention names every constraint."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user")
    role_assignments: Mapped[list["RoleAssignment"]] = relationship(
        back_populates="user", lazy="selectin"
    )


class RoleAssignment(Base):
    """Deployment-scoped role grant (task E0.7; spec 12.3).

    deployment_id is an opaque UUID with no foreign key until E1 creates the
    deployment table (phase-0 E0.7 fixes this explicitly); NULL means the
    grant is organization-wide. Role values come from app.auth.rbac.Role;
    stored as strings so adding a role never needs an enum migration.
    """

    __tablename__ = "role_assignment"
    __table_args__ = (UniqueConstraint("user_id", "role", "deployment_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"), index=True)
    role: Mapped[str] = mapped_column(String(40))
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
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
