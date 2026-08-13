"""E5.4b: the InfluxDB 3 tester, against a real InfluxDB 3.

Phase-5 fixed choice 5 splits the work deliberately. The **happy path runs
against the container**, because the things this tester exists to catch are
things a fake cannot have - Influx 3's actual auth semantics, its "a database
exists once it has been written to" behaviour, and a table drop that makes a
later query answer "not found" rather than "zero rows". The **error paths run
against in-process fake servers**, because inducing a malformed body or a 500
in a container means restarting it with a different config and in a handler it
is three lines.

The acceptance criteria this file is answerable to (phase document, E5.4b):
the write-then-delete leaves the reserved measurement with zero rows, asserted
by querying it afterwards; a wrong token fails `auth` and a wrong database
fails `not_found`, and the two are distinguishable in `CheckResult.detail`.
"""

import asyncio
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import free_port

from app.services.clients.influx import SELFTEST_MEASUREMENT, InfluxClient
from app.services.testers.base import ServiceCredentials
from app.services.testers.influx import InfluxTester

# `rig` is NOT imported: it is a fixture defined in conftest.py and pytest
# discovers it automatically. Importing it binds a SEPARATE fixture object
# into this module, and "session" scope then applies per copy - which built
# the five-container rig once per module and silently undid the phase-5
# section 5 gate-time design (measured: 15 containers, not 5).
pytestmark = pytest.mark.integration


def credentials(url: str, database: str, token: str) -> ServiceCredentials:
    return ServiceCredentials(
        service_key="influx",
        settings={"url": url, "database": database},
        secrets={"token": token},
    )


def checks_by_name(result) -> dict[str, object]:
    return {check.name: check for check in result.checks}


def run(tester: InfluxTester, creds: ServiceCredentials):
    return asyncio.run(tester.run(creds))


# --- against the real container ----------------------------------------------


def test_a_correctly_configured_influx_passes_every_check(rig):
    result = run(InfluxTester(), credentials(rig.influx.url, rig.influx_database, rig.influx_token))
    assert result.outcome == "pass", result.checks
    assert set(checks_by_name(result)) == {"query", "write", "cleanup"}
    assert all(check.passed for check in result.checks)


def test_the_reserved_measurement_is_empty_afterwards(rig):
    """The acceptance criterion, asserted from OUTSIDE the tester.

    The tester's own `cleanup` check queries the measurement, but a tester
    that both writes and grades its own cleanup could agree with itself. This
    asks Influx directly, on a fresh client, after the run.
    """
    run(InfluxTester(), credentials(rig.influx.url, rig.influx_database, rig.influx_token))

    client = InfluxClient(url=rig.influx.url, database=rig.influx_database, token=rig.influx_token)

    async def count() -> int:
        async with client.session() as session:
            return await client.count_rows(session, SELFTEST_MEASUREMENT)

    assert asyncio.run(count()) == 0


def test_the_tester_is_repeatable(rig):
    """Twice in a row passes. A tester that drops its own table and then fails
    on the second run because the table is gone would pass a one-shot suite."""
    for _ in range(2):
        result = run(
            InfluxTester(), credentials(rig.influx.url, rig.influx_database, rig.influx_token)
        )
        assert result.outcome == "pass", result.checks


def test_a_wrong_token_fails_auth_and_a_wrong_database_fails_not_found(rig):
    """Both halves of the acceptance criterion, and the DISTINGUISHABILITY it
    actually asks for: not merely that both fail, but that an operator can
    tell which of two completely different mistakes they made."""
    bad_token = run(
        InfluxTester(), credentials(rig.influx.url, rig.influx_database, "apiv3_not_a_real_token")
    )
    bad_database = run(
        InfluxTester(), credentials(rig.influx.url, "no_such_database", rig.influx_token)
    )

    assert bad_token.outcome == "fail"
    assert bad_database.outcome == "fail"

    token_detail = checks_by_name(bad_token)["query"].detail
    database_detail = checks_by_name(bad_database)["query"].detail
    assert token_detail != database_detail
    assert "token" in token_detail.lower()
    assert "no_such_database" in database_detail

    # And the remedies point at different actions, which is the whole reason
    # the two kinds are kept apart.
    assert checks_by_name(bad_token)["query"].remedy != (
        checks_by_name(bad_database)["query"].remedy
    )


def test_an_unreachable_influx_fails_with_a_remedy(rig):
    result = run(
        InfluxTester(),
        credentials(f"http://127.0.0.1:{free_port()}", rig.influx_database, "token"),
    )
    assert result.outcome == "fail"
    assert len(result.checks) == 1
    assert result.checks[0].remedy


def test_no_token_appears_in_any_result_or_log(rig, caplog):
    """Definition-of-done item: the credential is absent from every result.

    Asserted by scanning the serialized result for the literal token rather
    than by inspecting field names, which is the rule the phase document sets
    for every credential-bearing path.
    """
    with caplog.at_level("DEBUG"):
        result = run(
            InfluxTester(), credentials(rig.influx.url, rig.influx_database, rig.influx_token)
        )
    blob = repr(result) + "\n".join(record.getMessage() for record in caplog.records)
    assert rig.influx_token not in blob
    assert rig.influx_token[8:] not in blob


def test_every_failing_check_carries_a_remedy(rig):
    """E5.3 made `remedy` mandatory on failure; this is that rule applied to
    every failure path this module can reach."""
    runs = [
        run(InfluxTester(), credentials(rig.influx.url, rig.influx_database, "apiv3_wrong")),
        run(InfluxTester(), credentials(rig.influx.url, "nope", rig.influx_token)),
        run(InfluxTester(), credentials("not-a-url", rig.influx_database, rig.influx_token)),
    ]
    for result in runs:
        assert result.outcome == "fail"
        for check in result.checks:
            if not check.passed:
                assert check.remedy.strip(), (result, check)


# --- against in-process fakes ------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    responder: Callable[[str, str], tuple[int, bytes]] = staticmethod(
        lambda method, path: (200, b"[]")
    )

    def _reply(self) -> None:
        status, body = type(self).responder(self.command or "", self.path)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply
    do_DELETE = _reply

    def log_message(self, *args: object) -> None:  # keep the suite's output clean
        return


@pytest.fixture
def fake_influx() -> Iterator[Callable[[Callable[[str, str], tuple[int, bytes]]], str]]:
    """A one-request-at-a-time HTTP server whose answers a test chooses.

    In-process because phase-5 fixed choice 5 says the volume belongs here:
    a 200 carrying HTML, or a 500, is three lines in a handler and a container
    restart otherwise.
    """
    servers: list[HTTPServer] = []

    def start(responder: Callable[[str, str], tuple[int, bytes]]) -> str:
        handler = type("_Bound", (_Handler,), {"responder": staticmethod(responder)})
        server = HTTPServer(("127.0.0.1", 0), handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_a_200_that_is_not_json_is_a_failure_with_its_own_remedy(fake_influx):
    url = fake_influx(lambda method, path: (200, b"<html>sign in</html>"))
    result = run(InfluxTester(), credentials(url, "db", "token"))
    assert result.outcome == "fail"
    query = checks_by_name(result)["query"]
    assert "not JSON" in query.detail or "not a JSON" in query.detail.lower()
    assert "proxy" in query.remedy or "sign-in" in query.remedy


def test_a_200_returning_an_object_rather_than_rows_is_a_failure(fake_influx):
    url = fake_influx(lambda method, path: (200, b'{"results": []}'))
    result = run(InfluxTester(), credentials(url, "db", "token"))
    assert result.outcome == "fail"
    assert checks_by_name(result)["query"].remedy


def test_a_500_is_reported_with_its_status(fake_influx):
    url = fake_influx(lambda method, path: (500, b"boom"))
    result = run(InfluxTester(), credentials(url, "db", "token"))
    assert result.outcome == "fail"
    assert "500" in checks_by_name(result)["query"].detail


def test_a_read_only_token_fails_the_write_and_still_reports_the_query(fake_influx):
    """The shape of a token that can read and not write: `query` passes,
    `write` fails, and the remedy names write access rather than the token
    being wrong - which is a different thing for the operator to go and fix."""

    def responder(method: str, path: str) -> tuple[int, bytes]:
        if path.startswith("/api/v3/query_sql"):
            return 200, b"[]"
        if path.startswith("/api/v3/write_lp"):
            return 403, b"forbidden"
        return 200, b"[]"

    url = fake_influx(responder)
    result = run(InfluxTester(), credentials(url, "db", "token"))
    assert result.outcome == "fail"
    named = checks_by_name(result)
    assert named["query"].passed
    assert not named["write"].passed
    assert "write access" in named["write"].remedy


def test_the_body_of_an_error_cannot_leak_the_token_into_the_detail(fake_influx):
    """A service is free to echo whatever it likes; `redact` is the backstop.

    Some proxies echo the Authorization header into their error page. The
    tester renders `detail` straight into the S5 wizard, so the token must not
    survive the round trip even when the SERVER is the one leaking it.
    """
    token = "apiv3_supersecrettoken"
    url = fake_influx(lambda method, path: (500, f"upstream rejected Bearer {token}".encode()))
    result = run(InfluxTester(), credentials(url, "db", token))
    assert result.outcome == "fail"
    assert token not in repr(result)
    assert "[redacted]" in checks_by_name(result)["query"].detail
