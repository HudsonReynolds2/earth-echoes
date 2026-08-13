"""Dialling a deployment's InfluxDB 3 (task E5.4b; spec 16.2 row 2).

**The HTTP query API, not FlightSQL** (phase-5 task E5.4b, stated as a
constraint rather than a preference): FlightSQL would pull `pyarrow` and its
Arrow runtime into the platform image for a connectivity check that moves one
row. E7 may add a FlightSQL transport behind this same class when it needs to
move research-sized results; nothing above this module would change, which is
the point of fixed choice 8.

Three endpoints, all of them v3:

* `POST /api/v3/query_sql` - SQL in, JSON rows out;
* `POST /api/v3/write_lp` - line protocol in, 204 out, creating the database
  and the table on demand;
* `DELETE /api/v3/configure/table` - the only deletion InfluxDB 3 Core has.

**On "delete of a single test point": Influx 3 has no row-level delete, and
this is the API's semantics rather than an omission here.** Deletion in Core is
by table, so the reserved measurement is dropped whole - which is exactly
equivalent for a measurement nothing but this tester ever writes to, and is why
the measurement is reserved in the first place. `hard_delete=now` asks the
server to collect the data immediately instead of after its 24-hour grace
period. Measured against `influxdb:3-core` 3.11: the drop renames the catalog
entry to `_eoe_selftest-<timestamp>` and the hard delete then collects it in
the background, so a query for the reserved measurement afterwards answers
"table not found" rather than "zero rows". `count_rows` reads that as zero,
because to the operator - and to the acceptance criterion - "the reserved
measurement is empty" and "the reserved measurement does not exist" are the
same fact, and the alternative would be a tester that fails when it succeeded.
"""

from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.services.clients.httpbase import (
    DEFAULT_TIMEOUT_SECONDS,
    ServiceDialError,
    ServiceFailure,
    open_client,
    safe_endpoint,
    send,
    snippet,
    unexpected_status,
)

#: The measurement the connection test writes to and then drops. Underscored
#: and platform-prefixed so it cannot collide with a research table, and
#: constant so an operator who sees it in a catalog listing can search for it.
SELFTEST_MEASUREMENT = "_eoe_selftest"


@dataclass(frozen=True)
class InfluxClient:
    """One deployment's InfluxDB 3, dialable.

    `token` is `field(repr=False)` and `__str__` names only the endpoint and
    the database, following `BrokerCoordinates` (D66) and `MqttServiceClient`:
    an admin token in a log line is the worst leak in this phase, because it
    is the credential that can read every recording the deployment has ever
    analysed. Do not remove either.
    """

    url: str
    database: str
    token: str = field(repr=False, default="")
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __str__(self) -> str:
        return f"influx {safe_endpoint(self.url)} database {self.database}"

    @property
    def secrets(self) -> tuple[str, ...]:
        """What must never appear in operator-facing text from this client."""
        return (self.token,) if self.token else ()

    def endpoint(self, path: str) -> str:
        return f"{self.url.rstrip('/')}{path}"

    @asynccontextmanager
    async def session(self) -> Any:
        """One short-lived HTTP session carrying the bearer token.

        A context manager rather than a client attribute so a tester cannot
        leave a connection pool open behind a failed check; every method takes
        the session it is called with, which also lets one test drive several
        calls over one connection.
        """
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with open_client(headers=headers, timeout=self.timeout) as client:
            yield client

    def _failure(self, response: httpx.Response, url: str) -> ServiceFailure:
        """Classify a non-success status from any v3 endpoint.

        The two the acceptance criterion names are `auth` and `not_found`, and
        they have to be distinguishable in `CheckResult.detail` - so they are
        distinguishable here first, by kind, and the detail is derived from
        the kind rather than the other way round.
        """
        body = snippet(response, self.secrets)
        where = safe_endpoint(url)
        if response.status_code in (401, 403):
            return ServiceFailure(
                kind="auth",
                detail=f"{where} rejected the admin token (HTTP {response.status_code})",
                remedy=(
                    "check the InfluxDB 3 admin token. A token is shown once, when it is "
                    "created; if it has been lost, create a new admin token with "
                    "'influxdb3 create token --admin' and paste that"
                ),
            )
        if response.status_code == 404 and "database not found" in body:
            return ServiceFailure(
                kind="not_found",
                detail=(f"{where} has no database named '{self.database}' (HTTP 404: {body})"),
                remedy=(
                    f"create the database '{self.database}' on this server, or correct the "
                    "database name on this form; InfluxDB 3 creates a database on first "
                    "write, so a database that has never been written to does not exist yet"
                ),
            )
        if response.status_code == 404:
            return ServiceFailure(
                kind="not_found",
                detail=f"{where} answered HTTP 404" + (f": {body}" if body else ""),
                remedy=(
                    "check that this URL is an InfluxDB 3 server's own API endpoint. "
                    "InfluxDB 1.x and 2.x do not serve the /api/v3 routes this platform uses"
                ),
            )
        return unexpected_status(response, url, self.secrets)

    async def query(self, client: httpx.AsyncClient, sql: str) -> list[dict[str, Any]]:
        """Run one SQL statement against the configured database.

        Raises `ServiceDialError` for anything that is not a 200 with a JSON
        array, including a 200 whose body is not what the v3 API documents -
        which is what a reverse proxy or a captive portal in front of Influx
        looks like, and is worth its own remedy rather than a stack trace.
        """
        url = self.endpoint("/api/v3/query_sql")
        response = await send(
            client,
            "POST",
            url,
            json={"db": self.database, "q": sql, "format": "json"},
        )
        if response.status_code != 200:
            raise ServiceDialError(self._failure(response, url))
        try:
            payload = response.json()
        except ValueError as error:
            raise ServiceDialError(
                ServiceFailure(
                    kind="bad_response",
                    detail=(
                        f"{safe_endpoint(url)} answered 200 with a body that is not JSON "
                        f"({type(error).__name__})"
                    ),
                    remedy=(
                        "check that this URL reaches InfluxDB 3 directly rather than a proxy "
                        "or sign-in page that answers 200 for everything"
                    ),
                )
            ) from error
        if not isinstance(payload, list):
            raise ServiceDialError(
                ServiceFailure(
                    kind="bad_response",
                    detail=(
                        f"{safe_endpoint(url)} answered 200 with a "
                        f"{type(payload).__name__} where the v3 query API returns a list of rows"
                    ),
                    remedy=(
                        "check that this URL reaches InfluxDB 3 directly rather than another "
                        "service that happens to answer on this port"
                    ),
                )
            )
        return [row for row in payload if isinstance(row, dict)]

    async def write_point(
        self, client: httpx.AsyncClient, measurement: str, tags: Mapping[str, str], value: int
    ) -> None:
        """Write one point of line protocol, letting the server timestamp it.

        No explicit timestamp on purpose: a point stamped by the platform's
        clock lands outside the retention window of a deployment whose clock
        has drifted, and then the read-back that proves the write worked finds
        nothing. The server's own clock is always inside its own retention.
        """
        url = self.endpoint("/api/v3/write_lp")
        line = "".join(
            [measurement, *(f",{key}={_escape(val)}" for key, val in sorted(tags.items()))]
        )
        response = await send(
            client,
            "POST",
            url,
            params={"db": self.database, "precision": "nanosecond"},
            content=f"{line} value={value}i".encode(),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        if response.status_code not in (200, 204):
            raise ServiceDialError(self._failure(response, url))

    async def delete_measurement(self, client: httpx.AsyncClient, measurement: str) -> bool:
        """Drop one table. Returns False if it was not there to drop.

        A 404 is not an error here: a tester whose write failed still tries to
        clean up, and "there was nothing to delete" is the outcome it wants,
        not a second failure stacked on the first.
        """
        url = self.endpoint("/api/v3/configure/table")
        response = await send(
            client,
            "DELETE",
            url,
            params={"db": self.database, "table": measurement, "hard_delete": "now"},
        )
        if response.status_code == 404:
            return False
        if response.status_code not in (200, 202, 204):
            raise ServiceDialError(self._failure(response, url))
        return True

    async def count_rows(self, client: httpx.AsyncClient, measurement: str) -> int:
        """How many rows the measurement holds, with a dropped table as zero.

        See the module docstring: Influx 3 answers a query for a dropped table
        with a planning error, and to this tester that means the same thing as
        an empty one. Every other 400 is re-raised, so a genuinely broken
        query cannot be read as a clean measurement.
        """
        url = self.endpoint("/api/v3/query_sql")
        response = await send(
            client,
            "POST",
            url,
            json={
                "db": self.database,
                "q": f'SELECT COUNT(*) AS n FROM "{measurement}"',
                "format": "json",
            },
        )
        if response.status_code == 400 and "not found" in snippet(response, self.secrets):
            return 0
        if response.status_code != 200:
            raise ServiceDialError(self._failure(response, url))
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            return 0
        first = rows[0]
        count = first.get("n") if isinstance(first, dict) else None
        return int(count) if isinstance(count, int | float | str) else 0


def _escape(value: str) -> str:
    """Line-protocol escaping for a tag value (comma, space, equals)."""
    out: Iterator[str] = (("\\" + ch if ch in ", =" else ch) for ch in value)
    return "".join(out)
