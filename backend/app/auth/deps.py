"""Auth request dependencies (task E0.6; decisions D1 and D4).

`require_session` validates the signed cookie and loads a live session row;
`require_csrf` enforces the double-submit token on mutating endpoints. E0.7
layers the RBAC permission dependency on top of these.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.cookies import CSRF_HEADER, SESSION_COOKIE, unsign_session_id
from app.auth.service import load_valid_session
from app.errors import AppError
from app.models import UserSession


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    db = factory()
    try:
        yield db
    finally:
        db.close()


DbDep = Annotated[Session, Depends(get_db)]


def require_session(request: Request, db: DbDep) -> UserSession:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise AppError("unauthorized", "authentication required", status_code=401)
    session_id = unsign_session_id(cookie, request.app.state.settings.session_secret)
    if session_id is None:
        raise AppError("unauthorized", "invalid session", status_code=401)
    session = load_valid_session(db, session_id)
    if session is None or not session.user.is_active:
        raise AppError("unauthorized", "session expired or revoked", status_code=401)
    return session


SessionDep = Annotated[UserSession, Depends(require_session)]


def require_csrf(request: Request, session: SessionDep) -> UserSession:
    header = request.headers.get(CSRF_HEADER)
    if not header or header != session.csrf_token:
        raise AppError("forbidden", "CSRF token missing or invalid", status_code=403)
    return session


CsrfSessionDep = Annotated[UserSession, Depends(require_csrf)]
