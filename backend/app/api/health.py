"""Health endpoint (task E0.3): GET /api/v1/health with build and version info.

Includes a short-timeout database ping. An unreachable database degrades the
payload rather than failing the endpoint, matching the spec 14.3 philosophy of
marking things stale instead of failing the whole surface.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import create_engine, text

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    build_sha: str
    database: str


def _database_reachable(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        finally:
            engine.dispose()
    except Exception:
        return False


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        version="0.0.0",
        build_sha=settings.build_sha,
        database="ok" if _database_reachable(settings.database_url) else "unreachable",
    )
