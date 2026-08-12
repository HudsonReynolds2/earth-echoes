"""FastAPI application factory (task E0.3).

Everything rides the /api/v1 prefix; every error leaves through the envelope
(app.errors); every response carries a request id and the security-header
baseline. Serve with: uvicorn app.main:create_app --factory

E3.7 adds the lifespan. Two things may live alongside the API and both are
OFF unless asked for:

* An `MqttClientManager` for OUTBOUND publishes only — no subscriptions, so
  it never consumes anything the worker is also consuming. It exists because
  publishing is an HTTP action (`POST /revisions/{id}/publish`, and E2's
  apply at E3.13), and it is started only when `EOE_PUBLISH_ENABLED` is on:
  with the flag off nothing may reach a device anyway (D61), so a connection
  would be a socket held open to do something the platform has forbidden.
* The reconciliation worker itself, when `EOE_WORKER_IN_API` is on (D59) —
  the single-process deployment mode. Compose runs it as its own container.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.aggregators import router as aggregators_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.broker_credentials import router as broker_credentials_router
from app.api.config import router as config_router
from app.api.deployments import router as deployments_router
from app.api.entity_config import router as entity_config_router
from app.api.health import router as health_router
from app.api.imports import router as imports_router
from app.api.listeners import router as listeners_router
from app.api.organizations import router as organizations_router
from app.api.pods import router as pods_router
from app.api.revisions import router as revisions_router
from app.api.selections import router as selections_router
from app.api.services import router as services_router
from app.api.timeline import router as timeline_router
from app.api.totp import router as totp_router
from app.api.users import router as users_router
from app.api.ws import router as ws_router
from app.controlplane.broker import MqttClientManager, load_broker_coordinates
from app.controlplane.events import Hub, listen
from app.controlplane.runner import ReconciliationWorker
from app.db import create_session_factory
from app.errors import install_error_handlers
from app.middleware import RequestIdMiddleware, SecurityHeadersMiddleware, configure_logging
from app.secrets import SecretStore
from app.services.credentials import default_provider
from app.settings import Settings

API_PREFIX = "/api/v1"

log = logging.getLogger(__name__)


async def _refresh_forever(
    manager: MqttClientManager, interval: float, stopping: asyncio.Event
) -> None:
    """Poll `manager.refresh()` until shutdown (task E5.7b).

    The API's counterpart to `ReconciliationWorker._refresh_loop`. Two loops
    rather than one shared helper because the two processes stop differently —
    the worker owns an `_stopping` event its sweeps also watch, and the API has
    the lifespan's — and a helper taking both would be longer than either.

    **Waits first**, because the manager has just loaded its coordinates, so an
    immediate refresh would be a guaranteed no-op on every startup.

    A failure is logged and retried. The loader touches the database, and an
    API whose poll died on one blip would be permanently unable to publish to a
    deployment configured afterwards — with nothing about it visible from
    outside except revisions that stay `draft`.
    """
    while not stopping.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), interval)
        if stopping.is_set():
            return
        try:
            await manager.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("could not refresh broker coordinates; retrying next tick")


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start what the settings asked for, and stop it cleanly.

    `app.state.mqtt` is always SET, and is None when publishing is disabled —
    the publish route reads it and refuses rather than discovering a missing
    attribute. Startup never fails on a broker that is down: the manager's
    connection loop retries forever in the background (the E3.2 contract), so
    an unreachable broker degrades publishing, not the whole API.

    Nor does it fail on a DATABASE that is not ready. `start_or_retry` is what
    makes that true, and it is not a nicety: the API comes up beside Postgres
    in compose, so with publishing on it can reach the `deployment_service`
    query before the migrations have created the table. Awaiting a bare
    `start()` there kills uvicorn at startup (exit 3) and takes every route
    that has nothing to do with publishing down with it.
    """
    settings: Settings = app.state.settings
    app.state.mqtt = None
    app.state.worker = None
    refresh: asyncio.Task[None] | None = None
    # E3.12: the live-update fan-out. Always on — it needs no configuration
    # beyond the database the API already has (D59), and a websocket surface
    # that silently does nothing would be worse than one that is absent.
    app.state.hub = Hub()
    bus_stopping = asyncio.Event()
    bus = asyncio.create_task(
        listen(settings.database_url, app.state.hub.dispatch, stopping=bus_stopping),
        name="live-update-bus",
    )
    if settings.publish_enabled:
        manager = MqttClientManager(
            lambda: load_broker_coordinates(app.state.session_factory, app.state.secret_store)
        )
        await manager.start_or_retry()
        app.state.mqtt = manager
        # E5.7b: the API is the SECOND host of a manager, and it needs the
        # coordinates poll for the same reason the worker does. `POST
        # /revisions/{id}/publish` and E2's apply both publish through this
        # manager, so without a refresh a deployment configured after the API
        # started could never be published to from an HTTP request - the
        # operator would get `draft` for reasons nothing on screen explains.
        refresh = asyncio.create_task(
            _refresh_forever(manager, float(settings.broker_refresh_seconds), bus_stopping),
            name="mqtt-coordinates-refresh",
        )
        log.info("outbound publish connections started (EOE_PUBLISH_ENABLED is on)")
    if settings.worker_in_api:
        worker = ReconciliationWorker(
            app.state.session_factory,
            app.state.secret_store,
            timeout_interval=float(settings.timeout_sweep_seconds),
            drift_interval=float(settings.drift_sweep_seconds),
            service_config_interval=float(settings.service_config_sweep_seconds),
            broker_refresh_interval=float(settings.broker_refresh_seconds),
        )
        await worker.start()
        app.state.worker = worker
    try:
        yield
    finally:
        bus_stopping.set()
        bus.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await bus
        if refresh is not None:
            refresh.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await refresh
        if app.state.worker is not None:
            await app.state.worker.stop()
        if app.state.mqtt is not None:
            await app.state.mqtt.stop()


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
        lifespan=_lifespan,
    )
    app.state.settings = resolved
    engine, session_factory = create_session_factory(resolved.database_url)
    app.state.db_engine = engine
    app.state.session_factory = session_factory
    app.state.secret_store = SecretStore(session_factory, resolved.kek)
    # E5.6: how per-device broker credentials are minted and revoked. On
    # `app.state` rather than imported at each call site so a test can
    # substitute one without a broker, exactly as `secret_store` and `mqtt`
    # already work. Fixed choice 4 means there is no setting that selects a
    # different one in production.
    app.state.credential_provider = default_provider()

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
    api_router.include_router(broker_credentials_router)
    api_router.include_router(listeners_router)
    api_router.include_router(imports_router)
    api_router.include_router(config_router)
    api_router.include_router(entity_config_router)
    api_router.include_router(selections_router)
    api_router.include_router(services_router)
    api_router.include_router(revisions_router)
    api_router.include_router(timeline_router)
    api_router.include_router(ws_router)
    app.include_router(api_router)

    return app
