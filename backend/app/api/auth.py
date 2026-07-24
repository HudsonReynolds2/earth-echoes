"""Auth endpoints (task E0.6; spec 12.2, 13): POST /auth/login,
POST /auth/logout, GET /auth/me.

Login sets two cookies: the HttpOnly signed session cookie and the
JS-readable CSRF cookie (double-submit, D4). Responses never contain the
password or its hash. Bad credentials return one indistinguishable 401.
E0.8 wires the audit hook onto these mutations when it lands.
"""

import uuid

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, EmailStr

from app.auth.cookies import CSRF_COOKIE, SESSION_COOKIE, sign_session_id
from app.auth.deps import CsrfSessionDep, DbDep, SessionDep
from app.auth.service import authenticate, create_session, revoke_session
from app.errors import AppError

router = APIRouter(prefix="/auth")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AssignmentResponse(BaseModel):
    role: str
    deployment_id: uuid.UUID | None


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    is_active: bool
    assignments: list[AssignmentResponse] = []


def _cookie_secure(request: Request) -> bool:
    # Secure except on plain-HTTP localhost (decision D4).
    return request.url.scheme == "https"


def _set_auth_cookies(
    request: Request, response: Response, session_id: str, csrf_token: str, max_age: int
) -> None:
    secure = _cookie_secure(request)
    response.set_cookie(
        SESSION_COOKIE,
        sign_session_id(session_id, request.app.state.settings.session_secret),
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        httponly=False,  # double-submit: the frontend echoes it in X-CSRF-Token
        samesite="lax",
        secure=secure,
        path="/",
    )


@router.post("/login", response_model=UserResponse)
def login(body: LoginRequest, request: Request, response: Response, db: DbDep) -> UserResponse:
    user = authenticate(db, body.email, body.password)
    if user is None:
        raise AppError("unauthorized", "invalid credentials", status_code=401)
    settings = request.app.state.settings
    session = create_session(
        db,
        user,
        ttl_seconds=settings.session_ttl_seconds,
        user_agent=request.headers.get("user-agent", ""),
        ip=request.client.host if request.client else "",
    )
    _set_auth_cookies(
        request, response, session.id, session.csrf_token, settings.session_ttl_seconds
    )
    return UserResponse(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        assignments=[
            AssignmentResponse(role=a.role, deployment_id=a.deployment_id)
            for a in user.role_assignments
        ],
    )


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: DbDep, session: CsrfSessionDep) -> None:
    revoke_session(db, session)
    secure = _cookie_secure(request)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=secure)
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="lax", secure=secure)


@router.get("/me", response_model=UserResponse)
def me(session: SessionDep) -> UserResponse:
    user = session.user
    return UserResponse(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        assignments=[
            AssignmentResponse(role=a.role, deployment_id=a.deployment_id)
            for a in user.role_assignments
        ],
    )
