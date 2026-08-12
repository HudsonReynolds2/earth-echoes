"""The Prometheus connection tester (task E5.4c; spec 16.2 row 3).

Two checks, because there are two endpoints with two roles and they fail
independently:

1. **`read_query`** - an authenticated `up` instant query against the read URL.
   `up` because every Prometheus scraping anything has it, so a healthy server
   never answers this with an empty result for a reason the operator has to
   think about.
2. **`remote_write`** - the receiver probe. Enabled-and-accepting, credentials
   rejected, or receiver switched off, and each one says which.

**The receiver being off by default is the failure this unit exists for.**
An operator who has never passed `--web.enable-remote-write-receiver` has a
Prometheus that answers reads perfectly and silently accepts no metrics from
any Aggregator, forever. Measured against `prom/prometheus:v3.5.0`: with the
flag and good credentials the write is `204`; without the flag it is `404`
carrying "remote write receiver needs to be enabled with
--web.enable-remote-write-receiver"; with bad credentials it is `401` **on both
builds**, because Prometheus checks basic auth before it routes. That ordering
is why the probe reads 401 before it reads 404 - reversed, a wrong password
against a correctly-configured server would be reported as a missing receiver
and send the operator to edit the wrong file.

**The probe writes no samples.** It sends a well-formed, empty remote-write
body, so nothing this tester does can appear in an operator's monitoring data.
`test_tester_prometheus.py` asserts that explicitly rather than leaving it as
an implementation detail, so changing it later has to be deliberate.
"""

import time
from dataclasses import dataclass

from app.services.clients.httpbase import ServiceDialError
from app.services.clients.prometheus import PrometheusClient, series_names
from app.services.testers.base import (
    CheckResult,
    ServiceCredentials,
    TestResult,
)

#: Two HTTP round trips against a service that answers from memory.
PROMETHEUS_BUDGET_SECONDS = 12.0


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def client_for(credentials: ServiceCredentials) -> PrometheusClient:
    settings = credentials.settings
    return PrometheusClient(
        read_url=str(settings["read_url"]),
        remote_write_url=str(settings["remote_write_url"]),
        remote_write_user=str(settings.get("remote_write_user") or ""),
        remote_write_password=credentials.secrets.get("remote_write_password", ""),
    )


@dataclass
class PrometheusTester:
    """Spec 16.2's Prometheus test. Registered as `REGISTRY["prometheus"]`."""

    service_key: str = "prometheus"
    budget_seconds: float = PROMETHEUS_BUDGET_SECONDS

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
                            "the stored Prometheus settings are incomplete "
                            f"({type(error).__name__})"
                        ),
                        remedy=(
                            "re-enter the Prometheus read URL, remote-write URL and credentials, "
                            "save them, then test again"
                        ),
                        elapsed_ms=0,
                    ),
                ),
            )

        checks: list[CheckResult] = []
        async with client.session() as session:
            started = time.monotonic()
            try:
                rows = await client.instant_query(session, "up")
            except ServiceDialError as error:
                checks.append(
                    CheckResult(
                        name="read_query",
                        passed=False,
                        detail=error.failure.detail,
                        remedy=error.failure.remedy,
                        elapsed_ms=_ms(started),
                    )
                )
            else:
                names = series_names(rows)
                checks.append(
                    CheckResult(
                        name="read_query",
                        passed=True,
                        detail=(
                            f"{client} answered an authenticated 'up' query with "
                            f"{len(names)} series"
                        ),
                        remedy="",
                        elapsed_ms=_ms(started),
                    )
                )

            probe_started = time.monotonic()
            try:
                _verdict, failure = await client.remote_write_probe(session)
            except ServiceDialError as error:
                checks.append(
                    CheckResult(
                        name="remote_write",
                        passed=False,
                        detail=error.failure.detail,
                        remedy=error.failure.remedy,
                        elapsed_ms=_ms(probe_started),
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        name="remote_write",
                        passed=failure is None,
                        detail=(
                            "the remote-write receiver is enabled and accepted these credentials"
                            if failure is None
                            else failure.detail
                        ),
                        remedy="" if failure is None else failure.remedy,
                        elapsed_ms=_ms(probe_started),
                    )
                )

        return TestResult(
            service_key=self.service_key,
            outcome="pass" if all(check.passed for check in checks) else "fail",
            checks=tuple(checks),
        )
