"""Dialling a deployment's Grafana (task E5.4d; spec 16.2 row 4).

**The one client in this package that WRITES**, and the rules that follow from
that are the substance of the module:

* **Provisioning is never a side effect of a test.** `datasources()` reads and
  reports what is missing; `provision_datasource` and `ensure_contact_point`
  are separate calls a caller makes deliberately. A connection test that
  quietly created objects in an operator's Grafana would be indistinguishable
  from a misconfiguration the next person has to unpick.
* **Every write is idempotent by lookup-then-decide, never blind POST.** This
  is a system a human also edits, so running the tester twice must leave one
  contact point and one datasource per store - asserted in the suite by
  diffing Grafana's OWN listing rather than by trusting these methods.

**On the alert webhook contact point.** Its URL points at the platform's
`POST /webhooks/grafana-alerts`, which **E7.6 implements and this phase does
not** (phase-5 section 3 says so explicitly). Registering a contact point that
targets a route which does not exist yet is deliberate, not an oversight: spec
16.2 makes the registration part of onboarding, and Grafana does not dial the
URL until an alert actually fires. The comment at `contact_point_payload` is
the one the phase document asks be left at the call site.
"""

from collections.abc import Mapping, Sequence
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

#: The contact point the platform owns. Constant so the second run finds the
#: first run's object; named so an operator can see whose it is.
CONTACT_POINT_NAME = "eoe-platform-alerts"

#: The platform route Grafana will POST alerts to. **E7.6 implements this and
#: E5 does not** (phase-5 section 3). It is registered now because spec 16.2
#: makes it part of onboarding and Grafana only dials it when an alert fires,
#: so a contact point pointing at a not-yet-built route is inert rather than
#: broken.
WEBHOOK_PATH = "/webhooks/grafana-alerts"

#: The datasources the platform expects a deployment's Grafana to have, by
#: type. Values are the `type` Grafana knows them by.
EXPECTED_DATASOURCES: Mapping[str, str] = {
    "influx": "influxdb",
    "prometheus": "prometheus",
}


@dataclass(frozen=True)
class GrafanaClient:
    """One deployment's Grafana, dialable with a service account token."""

    base_url: str
    token: str = field(repr=False, default="")
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __str__(self) -> str:
        return f"grafana {safe_endpoint(self.base_url)}"

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.token,) if self.token else ()

    def endpoint(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    @asynccontextmanager
    async def session(self) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with open_client(headers=headers, timeout=self.timeout) as client:
            yield client

    def _failure(self, response: httpx.Response, url: str) -> ServiceFailure:
        if response.status_code in (401, 403):
            return ServiceFailure(
                kind="auth",
                detail=(
                    f"{safe_endpoint(url)} rejected the service account token "
                    f"(HTTP {response.status_code})"
                ),
                remedy=(
                    "check the Grafana service account token, and that its account has the "
                    "Admin role - reading datasources and registering a contact point both "
                    "need it. Create one under Administration > Service accounts"
                ),
            )
        return unexpected_status(response, url, self.secrets)

    async def health(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """`/api/health`, which Grafana serves unauthenticated.

        Checked before anything else precisely BECAUSE it needs no token: it
        separates "this URL is not a Grafana" from "this token is wrong", and
        those are different things for the operator to go and fix.
        """
        url = self.endpoint("/api/health")
        response = await send(client, "GET", url)
        if response.status_code != 200:
            raise ServiceDialError(self._failure(response, url))
        try:
            payload = response.json()
        except ValueError as error:
            raise ServiceDialError(
                ServiceFailure(
                    kind="bad_response",
                    detail=f"{safe_endpoint(url)} answered 200 with a body that is not JSON",
                    remedy=(
                        "check that this URL is a Grafana server's root rather than a proxy or "
                        "sign-in page in front of it"
                    ),
                )
            ) from error
        if not isinstance(payload, dict) or "database" not in payload:
            raise ServiceDialError(
                ServiceFailure(
                    kind="bad_response",
                    detail=(
                        f"{safe_endpoint(url)} answered 200 but not with Grafana's health "
                        f"document ({snippet(response, self.secrets)})"
                    ),
                    remedy=(
                        "check that this URL is Grafana's own base URL; the platform appends "
                        "/api/health itself"
                    ),
                )
            )
        return payload

    async def datasources(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Every datasource this token can see. Raises on auth failure."""
        url = self.endpoint("/api/datasources")
        response = await send(client, "GET", url)
        if response.status_code != 200:
            raise ServiceDialError(self._failure(response, url))
        payload = response.json()
        return (
            [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
        )

    async def contact_points(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Every Alertmanager contact point (Grafana 9+ provisioning API)."""
        url = self.endpoint("/api/v1/provisioning/contact-points")
        response = await send(client, "GET", url)
        if response.status_code != 200:
            raise ServiceDialError(self._failure(response, url))
        payload = response.json()
        return (
            [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
        )

    def contact_point_payload(self, platform_base_url: str) -> dict[str, Any]:
        """The platform's webhook contact point.

        **The `url` points at `POST /webhooks/grafana-alerts`, which E7.6 builds
        and this phase does not.** See the module docstring: the route not
        existing yet is deliberate and harmless, because Grafana dials a
        contact point only when an alert fires, and spec 11.1 says the platform
        does not author alert rules in v1.
        """
        return {
            "name": CONTACT_POINT_NAME,
            "type": "webhook",
            "settings": {"url": f"{platform_base_url.rstrip('/')}{WEBHOOK_PATH}"},
        }

    async def ensure_contact_point(
        self, client: httpx.AsyncClient, platform_base_url: str
    ) -> tuple[str, int]:
        """Create the platform's contact point if it is not already there.

        Returns `(action, total_with_our_name)` where action is `created` or
        `present`. **Looks before it writes**, so a second run is a no-op: this
        is the one mutation in the phase and the operator edits the same
        object by hand.
        """
        existing = await self.contact_points(client)
        ours = [row for row in existing if row.get("name") == CONTACT_POINT_NAME]
        if ours:
            return "present", len(ours)

        url = self.endpoint("/api/v1/provisioning/contact-points")
        response = await send(
            client,
            "POST",
            url,
            json=self.contact_point_payload(platform_base_url),
            headers={"X-Disable-Provenance": "true"},
        )
        if response.status_code not in (200, 201, 202):
            raise ServiceDialError(self._failure(response, url))
        after = await self.contact_points(client)
        return "created", len([row for row in after if row.get("name") == CONTACT_POINT_NAME])

    async def provision_datasource(
        self, client: httpx.AsyncClient, name: str, ds_type: str, url_value: str
    ) -> str:
        """Create one datasource if absent. Returns `created` or `present`.

        A deliberate, separate call - never reached from `run`. Spec 16.2
        offers provisioning; the phase document requires that offering it and
        doing it stay two different actions.
        """
        existing = await self.datasources(client)
        if any(row.get("type") == ds_type for row in existing):
            return "present"
        endpoint = self.endpoint("/api/datasources")
        response = await send(
            client,
            "POST",
            endpoint,
            json={"name": name, "type": ds_type, "url": url_value, "access": "proxy"},
        )
        if response.status_code in (409,):
            return "present"
        if response.status_code not in (200, 201):
            raise ServiceDialError(self._failure(response, endpoint))
        return "created"


def missing_datasources(existing: Sequence[Mapping[str, Any]]) -> list[str]:
    """Which of `EXPECTED_DATASOURCES` this Grafana does not have.

    By `type` and not by name, because an operator names their datasources
    whatever they like and the platform has no business insisting.
    """
    present = {row.get("type") for row in existing}
    return sorted(key for key, ds_type in EXPECTED_DATASOURCES.items() if ds_type not in present)
