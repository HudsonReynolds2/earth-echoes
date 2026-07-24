"""Error envelope (task E0.3; phase-0 section 2).

The only error shape this API ever returns:
    {"error": {"code": str, "message": str, "detail": object | null}}

The code vocabulary is decision D8: stable, snake_case, never renamed. It may
extend; existing codes never change.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

ERROR_CODES = frozenset(
    {
        "validation_error",
        "unauthorized",
        "forbidden",
        "not_found",
        "method_not_allowed",
        "conflict",
        "internal_error",
    }
)

_STATUS_TO_CODE = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
}


class AppError(Exception):
    """Domain error carrying an envelope code; raise anywhere below the API."""

    def __init__(self, code: str, message: str, status_code: int = 400, detail: Any = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}; extend ERROR_CODES deliberately (D8)")
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)


def envelope(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, error: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content=envelope(error.code, error.message, error.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, error: RequestValidationError) -> JSONResponse:
        fields = [
            {"loc": [str(part) for part in item.get("loc", [])], "message": item.get("msg", "")}
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=envelope("validation_error", "request validation failed", {"fields": fields}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_TO_CODE.get(error.status_code, "internal_error")
        return JSONResponse(
            status_code=error.status_code,
            content=envelope(code, str(error.detail)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, error: Exception) -> JSONResponse:
        # Never leak internals; the request id in the response headers is the
        # correlation handle for the server-side log record.
        return JSONResponse(
            status_code=500,
            content=envelope("internal_error", "internal server error"),
        )
