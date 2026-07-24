"""Optional TOTP (task E0.10; spec 12.2, 13): enrollment and confirmation
under /auth/totp. Off by default; enrollment is user-initiated. The secret
lives only in SecretStore under totp:{user_id} (project-changes #5); this
surface returns it exactly once, at enrollment, for the authenticator app."""

from fastapi import APIRouter, Request
from pydantic import BaseModel
from pyotp import TOTP, random_base32

from app.audit import record_audit
from app.auth.deps import CsrfSessionDep, DbDep
from app.errors import AppError
from app.models import User
from app.secrets import SecretStore

router = APIRouter(prefix="/auth/totp")

ISSUER = "Echoes of Earth"


def secret_name(user_id: object) -> str:
    return f"totp:{user_id}"


def _store(request: Request) -> SecretStore:
    store: SecretStore = request.app.state.secret_store
    return store


class EnrollResponse(BaseModel):
    secret: str
    otpauth_url: str


class ConfirmRequest(BaseModel):
    code: str


class TotpStatus(BaseModel):
    totp_enabled: bool


@router.post("/enroll", response_model=EnrollResponse)
def enroll(request: Request, session: CsrfSessionDep, db: DbDep) -> EnrollResponse:
    user = session.user
    if user.totp_enabled:
        raise AppError("conflict", "TOTP is already enabled", status_code=409)
    secret = random_base32()
    _store(request).put(secret_name(user.id), secret)
    record_audit(
        db,
        action="auth.totp_enroll",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=user.id,
    )
    db.commit()
    return EnrollResponse(
        secret=secret,
        otpauth_url=TOTP(secret).provisioning_uri(name=user.email, issuer_name=ISSUER),
    )


@router.post("/confirm", response_model=TotpStatus)
def confirm(
    body: ConfirmRequest, request: Request, session: CsrfSessionDep, db: DbDep
) -> TotpStatus:
    user = session.user
    if user.totp_enabled:
        raise AppError("conflict", "TOTP is already enabled", status_code=409)
    store = _store(request)
    name = secret_name(user.id)
    if not store.exists(name):
        raise AppError("conflict", "no enrollment in progress", status_code=409)
    if not TOTP(store.get(name)).verify(body.code, valid_window=1):
        raise AppError("validation_error", "invalid TOTP code", status_code=422)
    managed = db.get(User, user.id)
    if managed is None:  # pragma: no cover - session user always exists
        raise AppError("not_found", "no such user", status_code=404)
    managed.totp_enabled = True
    record_audit(
        db,
        action="auth.totp_enabled",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=user.id,
    )
    db.commit()
    return TotpStatus(totp_enabled=True)
