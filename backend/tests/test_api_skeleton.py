"""Gate 3: FastAPI skeleton checks (task E0.3).

Health payload, /api/v1 prefix discipline, the error envelope as the only
error shape, the D8 code vocabulary, request-id propagation (headers and
logs), CORS with credentials, and the security-header baseline.
"""

import logging

import pytest
from conftest import make_kek
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import ERROR_CODES, AppError
from app.main import API_PREFIX, create_app
from app.middleware import REQUEST_ID_HEADER, SECURITY_HEADERS
from app.settings import Settings

ALLOWED_ORIGIN = "http://allowed.test"

#: D8 fixed this vocabulary as stable and EXTENSIBLE: existing codes never
#: change, new ones are added deliberately and recorded. Extended once, by
#: E3.7 (D83), for a dependency outage the caller should retry.
D8_VOCABULARY = frozenset(
    {
        "validation_error",
        "unauthorized",
        "forbidden",
        "not_found",
        "method_not_allowed",
        "conflict",
        "internal_error",
        "service_unavailable",
    }
)


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://nobody:x@localhost:9/nowhere",
        session_secret="test-secret",
        kek=make_kek(),
        cors_origins=ALLOWED_ORIGIN,
    )


@pytest.fixture()
def app() -> FastAPI:
    application = create_app(make_settings())

    @application.get(f"{API_PREFIX}/_probe/echo")
    def echo(value: int) -> dict[str, int]:  # test-only route, added post-factory
        return {"value": value}

    @application.get(f"{API_PREFIX}/_probe/boom")
    def boom() -> None:
        raise RuntimeError("intentional")

    @application.get(f"{API_PREFIX}/_probe/conflict")
    def conflict() -> None:
        raise AppError("conflict", "already exists", status_code=409, detail={"id": "x"})

    @application.get(f"{API_PREFIX}/_probe/log")
    def log() -> dict[str, bool]:
        logging.getLogger("app.probe").info("probe log line")
        return {"logged": True}

    return application


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _assert_envelope(body: dict, code: str) -> None:
    assert set(body) == {"error"}, f"extra top-level keys: {set(body)}"
    assert set(body["error"]) == {"code", "message", "detail"}
    assert body["error"]["code"] == code
    assert body["error"]["code"] in D8_VOCABULARY


# --- check 1: health ---


def test_health_returns_build_and_version(client: TestClient):
    response = client.get(f"{API_PREFIX}/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["build_sha"] == "dev"
    assert body["database"] == "unreachable"  # deliberately dead URL in make_settings


# --- check 2: prefix discipline ---


def test_every_route_lives_under_the_versioned_prefix(client: TestClient):
    """Asserted through the public surface, not router internals: current
    FastAPI stores included routes lazily with prefixes applied at match time,
    so route objects carry unprefixed paths (DECISIONS D14)."""
    schema = client.get(f"{API_PREFIX}/openapi.json").json()
    paths = list(schema["paths"])
    assert f"{API_PREFIX}/health" in paths, "schema lost the health route"
    for path in paths:
        assert path.startswith(API_PREFIX), f"documented route escapes {API_PREFIX}: {path}"
    # Behavioral proof that nothing serves outside the prefix.
    assert client.get("/health").status_code == 404
    assert client.get("/").status_code == 404


# --- checks 3 and 4: the envelope is the only error shape ---


def test_envelope_on_404(client: TestClient):
    response = client.get(f"{API_PREFIX}/does-not-exist")
    assert response.status_code == 404
    _assert_envelope(response.json(), "not_found")


def test_envelope_on_405(client: TestClient):
    response = client.post(f"{API_PREFIX}/health")
    assert response.status_code == 405
    _assert_envelope(response.json(), "method_not_allowed")


def test_envelope_on_request_validation_with_field_detail(client: TestClient):
    response = client.get(f"{API_PREFIX}/_probe/echo", params={"value": "not-an-int"})
    assert response.status_code == 422
    body = response.json()
    _assert_envelope(body, "validation_error")
    fields = body["error"]["detail"]["fields"]
    assert fields and any("value" in field["loc"] for field in fields)


def test_envelope_on_unhandled_500_without_leak(client: TestClient):
    response = client.get(f"{API_PREFIX}/_probe/boom")
    assert response.status_code == 500
    body = response.json()
    _assert_envelope(body, "internal_error")
    assert "intentional" not in response.text, "internal detail leaked to the client"


def test_envelope_on_domain_error(client: TestClient):
    response = client.get(f"{API_PREFIX}/_probe/conflict")
    assert response.status_code == 409
    body = response.json()
    _assert_envelope(body, "conflict")
    assert body["error"]["detail"] == {"id": "x"}


# --- check 5: the code vocabulary is exactly D8 ---


def test_error_codes_are_exactly_the_d8_vocabulary():
    assert ERROR_CODES == D8_VOCABULARY


def test_unknown_code_is_rejected_at_raise_time():
    with pytest.raises(ValueError, match="unknown error code"):
        AppError("made_up_code", "nope")


# --- check 6: request id, four behaviors ---


def test_request_id_generated_when_absent(client: TestClient):
    response = client.get(f"{API_PREFIX}/health")
    assert response.headers.get(REQUEST_ID_HEADER)


def test_request_id_honors_inbound_header(client: TestClient):
    response = client.get(f"{API_PREFIX}/health", headers={REQUEST_ID_HEADER: "req-abc-123"})
    assert response.headers[REQUEST_ID_HEADER] == "req-abc-123"


def test_request_id_present_on_error_responses(client: TestClient):
    response = client.get(f"{API_PREFIX}/nowhere", headers={REQUEST_ID_HEADER: "req-err-1"})
    assert response.status_code == 404
    assert response.headers[REQUEST_ID_HEADER] == "req-err-1"


def test_request_id_bound_into_log_records(client: TestClient, caplog):
    with caplog.at_level(logging.INFO, logger="app.probe"):
        client.get(f"{API_PREFIX}/_probe/log", headers={REQUEST_ID_HEADER: "req-log-9"})
    records = [r for r in caplog.records if r.name == "app.probe"]
    assert records, "probe log line not captured"
    assert getattr(records[-1], "request_id", None) == "req-log-9"


# --- check 10: CORS with credentials ---


def test_cors_allows_configured_origin_with_credentials(client: TestClient):
    response = client.get(f"{API_PREFIX}/health", headers={"Origin": ALLOWED_ORIGIN})
    assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_cors_denies_unlisted_origin(client: TestClient):
    response = client.get(f"{API_PREFIX}/health", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in response.headers


# --- check 11: security headers on every response ---


def test_security_headers_on_success_and_error(client: TestClient):
    for path, expected_status in ((f"{API_PREFIX}/health", 200), (f"{API_PREFIX}/nope", 404)):
        response = client.get(path)
        assert response.status_code == expected_status
        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, f"{name} missing on {path}"


# --- check 12: OpenAPI schema ---


def test_openapi_schema_generates(client: TestClient):
    response = client.get(f"{API_PREFIX}/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"].startswith("3.")
    assert f"{API_PREFIX}/health" in schema["paths"]


# --- D127: the API process actually logs ------------------------------------


def test_creating_the_app_gives_the_root_logger_a_handler():
    """**Every `app.*` INFO line in the API was being dropped** (D127).

    Uvicorn attaches handlers to its own `uvicorn.*` loggers and leaves the
    ROOT logger bare, so Python's last-resort handler passed WARNING and above
    and silently discarded everything below — broker connected, coordinates
    refreshed, publish outcomes, all of it. `runner.py::main`'s docstring
    asserted the opposite, which is why it stood for so long.

    Found by C3's manual walkthrough: it tried to prove the refresh loop had
    connected by grepping the log, got nothing, and got nothing for a
    deployment that had been connected since startup either — while a WARNING
    from the same module was present. That asymmetry is what made it a logging
    bug rather than a behaviour bug.
    """
    import logging

    from app.middleware import install_root_handler

    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        root.handlers.clear()
        install_root_handler()
        assert root.handlers, "the root logger has no handler; app.* INFO goes nowhere"
        assert root.level <= logging.INFO
    finally:
        root.handlers[:] = saved


def test_an_app_info_line_reaches_a_handler(caplog):
    """The behavioural half: a real `app.*` INFO record is emitted rather than
    dropped. Asserted at the level the defect lived at — INFO — because
    WARNING was working the whole time and is what disguised it."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.controlplane.publisher"):
        logging.getLogger("app.controlplane.publisher").info("d127 probe line")
    assert any(record.message == "d127 probe line" for record in caplog.records)
