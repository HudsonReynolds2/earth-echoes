"""The InfluxDB 3 connection tester (task E5.4b; spec 16.2 row 2).

Three checks, in the order that makes each failure mean one thing:

1. **`query`** - an authenticated read against the configured database. This is
   where a wrong token and a wrong database name separate: `auth` and
   `not_found` are different kinds with different remedies, and the acceptance
   criterion is that they stay distinguishable in `CheckResult.detail`.
2. **`write`** - one point of line protocol into a reserved measurement. A read
   alone does not prove the platform can store telemetry, which is the only
   thing this service is here to do; an Influx token can be read-only.
3. **`cleanup`** - the reserved measurement is dropped and then queried again
   to prove it is gone. **A tester that writes into a research database and
   cannot clean up after itself is worse than no tester**, so the cleanup is a
   check the operator sees rather than a `finally` block nobody reads.

`SELECT 1` is the read, not a query against the reserved measurement: the
measurement legitimately does not exist before the first run, and a check whose
first-run failure is normal is a check that trains people to ignore it.
"""

import time
from dataclasses import dataclass

from app.services.clients.httpbase import ServiceDialError
from app.services.clients.influx import SELFTEST_MEASUREMENT, InfluxClient
from app.services.testers.base import (
    CheckResult,
    ServiceCredentials,
    TestResult,
)

#: Three HTTP round trips plus a table drop, which Influx does synchronously.
INFLUX_BUDGET_SECONDS = 15.0


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def client_for(credentials: ServiceCredentials) -> InfluxClient:
    """`ServiceCredentials` -> a dialable client. On the tester's side of the
    boundary for D116's reason: a client that imported the tester framework
    would close an import cycle through `testers/__init__`."""
    settings = credentials.settings
    return InfluxClient(
        url=str(settings["url"]),
        database=str(settings["database"]),
        token=credentials.secrets.get("token", ""),
    )


@dataclass
class InfluxTester:
    """Spec 16.2's Influx test. Registered as `REGISTRY["influx"]`."""

    service_key: str = "influx"
    budget_seconds: float = INFLUX_BUDGET_SECONDS

    async def run(self, credentials: ServiceCredentials) -> TestResult:
        try:
            client = client_for(credentials)
        except (KeyError, TypeError, ValueError) as error:
            return TestResult(
                service_key=self.service_key,
                outcome="fail",
                checks=(
                    CheckResult(
                        name="settings",
                        passed=False,
                        detail=(
                            f"the stored Influx settings are incomplete ({type(error).__name__})"
                        ),
                        remedy=(
                            "re-enter the InfluxDB 3 URL, database and admin token, save them, "
                            "then test again"
                        ),
                        elapsed_ms=0,
                    ),
                ),
            )

        checks: list[CheckResult] = []
        async with client.session() as session:
            started = time.monotonic()
            try:
                await client.query(session, "SELECT 1")
            except ServiceDialError as error:
                return TestResult(
                    service_key=self.service_key,
                    outcome="fail",
                    checks=(
                        CheckResult(
                            name="query",
                            passed=False,
                            detail=error.failure.detail,
                            remedy=error.failure.remedy,
                            elapsed_ms=_ms(started),
                        ),
                    ),
                )
            checks.append(
                CheckResult(
                    name="query",
                    passed=True,
                    detail=f"{client} answered an authenticated query",
                    remedy="",
                    elapsed_ms=_ms(started),
                )
            )

            write_started = time.monotonic()
            try:
                await client.write_point(session, SELFTEST_MEASUREMENT, {"source": "platform"}, 1)
                checks.append(
                    CheckResult(
                        name="write",
                        passed=True,
                        detail=(
                            f"wrote one point to the reserved measurement '{SELFTEST_MEASUREMENT}'"
                        ),
                        remedy="",
                        elapsed_ms=_ms(write_started),
                    )
                )
            except ServiceDialError as error:
                checks.append(
                    CheckResult(
                        name="write",
                        passed=False,
                        detail=error.failure.detail,
                        remedy=(
                            error.failure.remedy
                            if error.failure.kind != "auth"
                            else (
                                "this token can read but not write. Aggregators write telemetry "
                                "through it, so the platform needs a token with write access to "
                                f"the database '{client.database}'"
                            )
                        ),
                        elapsed_ms=_ms(write_started),
                    )
                )

            # Runs even when the write failed: a partial write still leaves a
            # table behind, and "we could not tidy up" is what the operator
            # needs to know about their research database.
            cleanup_started = time.monotonic()
            try:
                await client.delete_measurement(session, SELFTEST_MEASUREMENT)
                remaining = await client.count_rows(session, SELFTEST_MEASUREMENT)
            except ServiceDialError as error:
                checks.append(
                    CheckResult(
                        name="cleanup",
                        passed=False,
                        detail=error.failure.detail,
                        remedy=(
                            "the platform wrote a test point and could not remove it. Drop the "
                            f"measurement '{SELFTEST_MEASUREMENT}' from the database "
                            f"'{client.database}' by hand, and give the platform a token that "
                            "may delete tables"
                        ),
                        elapsed_ms=_ms(cleanup_started),
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        name="cleanup",
                        passed=remaining == 0,
                        detail=(
                            f"the reserved measurement '{SELFTEST_MEASUREMENT}' is empty"
                            if remaining == 0
                            else (
                                f"the reserved measurement '{SELFTEST_MEASUREMENT}' still holds "
                                f"{remaining} rows after being dropped"
                            )
                        ),
                        remedy=(
                            ""
                            if remaining == 0
                            else (
                                f"drop the measurement '{SELFTEST_MEASUREMENT}' from the database "
                                f"'{client.database}' by hand; the platform could not, and it is "
                                "the platform's own test data"
                            )
                        ),
                        elapsed_ms=_ms(cleanup_started),
                    )
                )

        return TestResult(
            service_key=self.service_key,
            outcome="pass" if all(check.passed for check in checks) else "fail",
            checks=tuple(checks),
        )
