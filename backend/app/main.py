"""FastAPI application factory (task E0.3).

Everything rides the /api/v1 prefix; every error leaves through the envelope
(app.errors); every response carries a request id and the security-header
baseline. Serve with: uvicorn app.main:create_app --factory
"""

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.aggregators import router as aggregators_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.deployments import router as deployments_router
from app.api.health import router as health_router
from app.api.imports import router as imports_router
from app.api.listeners import router as listeners_router
from app.api.organizations import router as organizations_router
from app.api.pods import router as pods_router
from app.api.totp import router as totp_router
from app.api.users import router as users_router
from app.db import create_session_factory
from app.errors import install_error_handlers
from app.middleware import RequestIdMiddleware, SecurityHeadersMiddleware, configure_logging
from app.secrets import SecretStore
from app.settings import Settings

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    # Settings() resolves its required fields from the environment and the
    # optional TOML file (D5); mypy cannot see those sources.
    resolved = settings if settings is not None else Settings()  # type: ignore[call-arg]
    configure_logging()

    app = FastAPI(
        title="Echoes of Earth Management Platform",
        version="0.0.0",
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
        redoc_url=None,
        # No swagger OAuth2 flow; without this FastAPI mounts a route at
        # /docs/oauth2-redirect, outside the versioned prefix.
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.settings = resolved
    engine, session_factory = create_session_factory(resolved.database_url)
    app.state.db_engine = engine
    app.state.session_factory = session_factory
    app.state.secret_store = SecretStore(session_factory, resolved.kek)

    # Order matters: security headers wrap everything, request id inside them,
    # CORS innermost so its headers survive on error responses too.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)
    if resolved.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )

    install_error_handlers(app)

    api_router = APIRouter(prefix=API_PREFIX)
    api_router.include_router(health_router)
    api_router.include_router(auth_router)
    api_router.include_router(totp_router)
    api_router.include_router(audit_router)
    api_router.include_router(users_router)
    api_router.include_router(organizations_router)
    api_router.include_router(deployments_router)
    api_router.include_router(pods_router)
    api_router.include_router(aggregators_router)
    api_router.include_router(listeners_router)
    api_router.include_router(imports_router)
    app.include_router(api_router)

    return app
