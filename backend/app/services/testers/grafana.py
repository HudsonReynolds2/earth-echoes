"""The Grafana connection tester (task E5.4d; spec 16.2 row 4).

Three checks:

1. **`health`** - `/api/health`, which Grafana serves WITHOUT a token. First on
   purpose: it separates "this URL is not a Grafana" from "this token is
   wrong", and those send the operator to two different places.
2. **`datasources`** - enumerate them and report which of Influx and Prometheus
   are missing. **Missing datasources do not fail the tester.** Spec 16.2 has
   this step *offer* to provision them, and an offer is not a verdict; the
   operator may also be pointing at a Grafana whose datasources are managed by
   file provisioning, which is a correct configuration this platform must not
   call broken. The check passes and its detail names what it would add.
3. **`contact_point`** - register the platform's alert webhook, idempotently.

**This is the only tester that changes the system it is testing**, so the
suite's real assertion is not "it worked" but "running it twice leaves exactly
one contact point", diffed against Grafana's own listing. `ensure_contact_point`
looks before it writes for that reason.

The contact point's URL targets `POST /webhooks/grafana-alerts`, **which E7.6
implements and this phase does not** - stated at the call site in
`clients/grafana.py::contact_point_payload`, as the phase document requires.
"""

import time
from dataclasses import dataclass, field

from app.services.clients.grafana import (
    CONTACT_POINT_NAME,
    GrafanaClient,
    missing_datasources,
)
from app.services.clients.httpbase import ServiceDialError
from app.services.testers.base import (
    CheckResult,
    ServiceCredentials,
    TestResult,
)

#: Grafana answers from its own database; three round trips is not slow, but
#: an under-resourced Grafana behind a proxy can be.
GRAFANA_BUDGET_SECONDS = 15.0

#: What the registered contact point points back at. A placeholder base URL,
#: not a deployment setting, because the platform's own public URL is E8's
#: concern and no epic owns it yet; E7.6 replaces this when it builds the
#: receiver. Wrong here is inert - Grafana dials a contact point only when an
#: alert fires, and spec 11.1 says v1 authors no alert rules.
DEFAULT_PLATFORM_BASE_URL = "http://localhost:8000"


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def client_for(credentials: ServiceCredentials) -> GrafanaClient:
    settings = credentials.settings
    return GrafanaClient(
        base_url=str(settings["base_url"]),
        token=credentials.secrets.get("service_account_token", ""),
    )


@dataclass
class GrafanaTester:
    """Spec 16.2's Grafana test. Registered as `REGISTRY["grafana"]`."""

    service_key: str = "grafana"
    budget_seconds: float = GRAFANA_BUDGET_SECONDS
    platform_base_url: str = field(default=DEFAULT_PLATFORM_BASE_URL)

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
                            f"the stored Grafana settings are incomplete ({type(error).__name__})"
                        ),
                        remedy=(
                            "re-enter the Grafana base URL and service account token, save them, "
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
                health = await client.health(session)
            except ServiceDialError as error:
                # Nothing else can mean anything if this URL is not a Grafana.
                return TestResult(
                    service_key=self.service_key,
                    outcome="fail",
                    checks=(
                        CheckResult(
                            name="health",
                            passed=False,
                            detail=error.failure.detail,
                            remedy=error.failure.remedy,
                            elapsed_ms=_ms(started),
                        ),
                    ),
                )
            checks.append(
                CheckResult(
                    name="health",
                    passed=True,
                    detail=(
                        f"{client} is healthy (database "
                        f"{health.get('database', 'unknown')}, version "
                        f"{health.get('version', 'unknown')})"
                    ),
                    remedy="",
                    elapsed_ms=_ms(started),
                )
            )

            ds_started = time.monotonic()
            try:
                existing = await client.datasources(session)
            except ServiceDialError as error:
                checks.append(
                    CheckResult(
                        name="datasources",
                        passed=False,
                        detail=error.failure.detail,
                        remedy=error.failure.remedy,
                        elapsed_ms=_ms(ds_started),
                    )
                )
            else:
                missing = missing_datasources(existing)
                checks.append(
                    CheckResult(
                        name="datasources",
                        # PASSES even when some are missing: spec 16.2 offers to
                        # provision them, and an offer is not a failure. A
                        # Grafana whose datasources come from file provisioning
                        # is correctly configured and must not be called broken.
                        passed=True,
                        detail=(
                            f"{len(existing)} datasources configured; "
                            + (
                                "Influx and Prometheus are both present"
                                if not missing
                                else f"the platform can provision {', '.join(missing)}"
                            )
                        ),
                        remedy="",
                        elapsed_ms=_ms(ds_started),
                    )
                )

            cp_started = time.monotonic()
            try:
                action, total = await client.ensure_contact_point(session, self.platform_base_url)
            except ServiceDialError as error:
                checks.append(
                    CheckResult(
                        name="contact_point",
                        passed=False,
                        detail=error.failure.detail,
                        remedy=error.failure.remedy,
                        elapsed_ms=_ms(cp_started),
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        name="contact_point",
                        # More than one is a real problem and worth failing on:
                        # duplicates mean every alert is delivered twice.
                        passed=total == 1,
                        detail=(
                            f"the alert contact point '{CONTACT_POINT_NAME}' is {action}"
                            if total == 1
                            else (
                                f"there are {total} contact points named "
                                f"'{CONTACT_POINT_NAME}' on this Grafana"
                            )
                        ),
                        remedy=(
                            ""
                            if total == 1
                            else (
                                f"remove the duplicate '{CONTACT_POINT_NAME}' contact points in "
                                "Grafana under Alerting > Contact points, leaving one; "
                                "duplicates deliver every alert more than once"
                            )
                        ),
                        elapsed_ms=_ms(cp_started),
                    )
                )

        return TestResult(
            service_key=self.service_key,
            outcome="pass" if all(check.passed for check in checks) else "fail",
            checks=tuple(checks),
        )
