"""Request-ID and security-header middleware (task E0.3).

The request id exists specifically to feed audit_log.request_id (E0.8): the
middleware honors an inbound X-Request-ID, generates one otherwise, echoes it
on every response, and binds it into log records through RequestIdFilter.
Security headers are the spec 14.1 baseline; E8.7 audits them later.
"""

import logging
import os
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


_factory_installed = False


def install_root_handler() -> None:
    """Give the ROOT logger a handler, so `app.*` INFO lines are visible (D139).

    **Uvicorn does not do this for us, and the codebase used to say it did.**
    Uvicorn's default config attaches handlers to its own `uvicorn.*` loggers
    and leaves the root logger bare, so Python's last-resort handler passes
    WARNING and above and silently drops everything below it. Every INFO line
    the API emits — broker connected, coordinates refreshed, publish outcomes —
    went nowhere, while `runner.py::main`'s docstring asserted the opposite.

    Found by C3's manual walkthrough, which tried to prove the refresh loop had
    connected by grepping the log and got nothing — including for a deployment
    that had been connected since startup. A WARNING from the same module was
    there, which is what made it a logging bug rather than a behaviour bug.

    `basicConfig` is a no-op when the root logger already has handlers, so a
    host that configured its own logging keeps it and the worker's own
    `basicConfig` is unaffected.
    """
    logging.basicConfig(
        level=os.environ.get("EOE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def configure_logging() -> None:
    """Stamp every log record with the current request id.

    Uses the log-record factory rather than a logging.Filter because filters
    bind to one logger or handler, while the factory covers every record from
    every logger, including libraries.
    """
    global _factory_installed
    if _factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.request_id = request_id_var.get()
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True
