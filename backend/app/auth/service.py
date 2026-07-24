"""Session lifecycle (task E0.6; decision D1)."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.cookies import new_token
from app.auth.passwords import verify_password
from app.models import User, UserSession, utcnow


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Return the user for valid credentials, else None.

    Unknown email and wrong password are indistinguishable to the caller (no
    user enumeration); a dummy verify keeps timing comparable.
    """
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        # Constant-shape work so response timing does not reveal existence.
        verify_password(
            "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$"
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            password,
        )
        return None
    if not user.is_active or not verify_password(user.password_hash, password):
        return None
    return user


def create_session(
    db: Session, user: User, ttl_seconds: int, user_agent: str = "", ip: str = ""
) -> UserSession:
    session = UserSession(
        id=new_token(),
        user_id=user.id,
        csrf_token=new_token(),
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        user_agent=user_agent[:400],
        ip=ip[:64],
    )
    db.add(session)
    # Flush, don't commit: the endpoint commits once, atomically with its
    # audit row (task E0.8).
    db.flush()
    return session


def load_valid_session(db: Session, session_id: str) -> UserSession | None:
    session = db.get(UserSession, session_id)
    if session is None or not session.is_valid():
        return None
    return session


def revoke_session(db: Session, session: UserSession) -> None:
    session.revoked_at = utcnow()
    db.flush()  # endpoint commits atomically with its audit row (E0.8)
