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


#: The service account the platform mints for itself, and the name of the token
#: it issues to that account. Constant so a second bootstrap finds the first
#: one's account instead of accumulating one per attempt, and named so an
#: operator looking at Administration > Service accounts can see whose it is
#: and revoke it in one click.
SERVICE_ACCOUNT_NAME = "echoes-platform"
SERVICE_ACCOUNT_CREDENTIAL_NAME = "echoes-platform-token"

#: Grafana's own role vocabulary. Admin because the two things the platform
#: does through this account — reading datasources and registering a contact
#: point through the provisioning API — both require it; Editor is not enough
#: for `/api/v1/provisioning`, which is the mistake this constant exists to
#: stop being made again.
SERVICE_ACCOUNT_ROLE = "Admin"


@dataclass(frozen=True)
class GrafanaAdminClient:
    """A Grafana dialled with an ADMIN USERNAME AND PASSWORD, for one job.

    **This is a bootstrap credential and it is used once.** Grafana will not
    accept a service account token whose value someone else chose: token values
    are issued by Grafana at runtime and shown once. So a stack the platform
    generated cannot be handed a token in advance — the only credential a fresh
    Grafana can be given up front is `GF_SECURITY_ADMIN_USER` / `_PASSWORD`.
    The platform therefore generates an admin account, and the first
    verification uses it to create a scoped service account and have Grafana
    issue a token for it. Everything after that — every test, every contact
    point — dials with that token; the admin password is never sent again.

    That is also what an operator with their OWN Grafana gets if they supply an
    admin account instead of a token, which is the same flow and not a special
    case: `provision.ensure_grafana_service_account` does not know or care
    which of the two produced the credential it was handed.

    Basic auth rather than a bearer header is not a downgrade — it is the only
    scheme Grafana accepts for a username and password, measured: `Bearer
    <admin password>` answers 401 on `/api/datasources` and basic auth answers
    200 on both the endpoints involved.
    """

    base_url: str
    username: str
    password: str = field(repr=False, default="")
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __str__(self) -> str:
        return f"grafana {safe_endpoint(self.base_url)}"

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.password,) if self.password else ()

    def endpoint(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    @asynccontextmanager
    async def session(self) -> Any:
        async with open_client(auth=(self.username, self.password), timeout=self.timeout) as client:
            yield client

    def _failure(self, response: httpx.Response, url: str) -> ServiceFailure:
        if response.status_code in (401, 403):
            return ServiceFailure(
                kind="auth",
                detail=(
                    f"{safe_endpoint(url)} rejected the Grafana admin account "
                    f"(HTTP {response.status_code})"
                ),
                remedy=(
                    "check the Grafana admin username and password. On a stack this platform "
                    "generated they are GF_SECURITY_ADMIN_USER and GF_SECURITY_ADMIN_PASSWORD "
                    "in the bundle's .env, and an operator who has changed the admin password "
                    "in Grafana has to save the new one here"
                ),
            )
        return unexpected_status(response, url, self.secrets)

    async def _find_service_account(self, client: httpx.AsyncClient) -> int | None:
        """The id of our service account, or None. Searched by exact name and
        not by the search endpoint's fuzzy match, so an operator's own account
        called `echoes-platform-staging` is never mistaken for ours."""
        url = self.endpoint("/api/serviceaccounts/search")
        response = await send(client, "GET", url, params={"query": SERVICE_ACCOUNT_NAME})
        if response.status_code != 200:
            raise ServiceDialError(self._failure(response, url))
        payload = response.json()
        accounts = payload.get("serviceAccounts", []) if isinstance(payload, dict) else []
        for account in accounts:
            if isinstance(account, dict) and account.get("name") == SERVICE_ACCOUNT_NAME:
                found = account.get("id")
                if isinstance(found, int):
                    return found
        return None

    async def ensure_service_account(self, client: httpx.AsyncClient) -> int:
        """Our service account's id, creating it if this is the first run.

        Lookup-then-decide, the same rule `ensure_contact_point` follows: an
        operator can bring the stack down and up, or rotate, without collecting
        a drawer full of identical service accounts.
        """
        existing = await self._find_service_account(client)
        if existing is not None:
            return existing
        url = self.endpoint("/api/serviceaccounts")
        response = await send(
            client, "POST", url, json={"name": SERVICE_ACCOUNT_NAME, "role": SERVICE_ACCOUNT_ROLE}
        )
        if response.status_code not in (200, 201):
            # A concurrent bootstrap may have won the race; the account it made
            # is as good as ours, and two callers must not end with two.
            if response.status_code == 409:
                found = await self._find_service_account(client)
                if found is not None:
                    return found
            raise ServiceDialError(self._failure(response, url))
        payload = response.json()
        account_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(account_id, int):
            raise ServiceDialError(
                ServiceFailure(
                    kind="bad_response",
                    detail=f"{self} created a service account and returned no id",
                    remedy=(
                        "check the Grafana version; service account provisioning needs "
                        "Grafana 9 or later"
                    ),
                )
            )
        return account_id

    async def issue_token(self, client: httpx.AsyncClient, account_id: int) -> str:
        """Have Grafana issue a token for our service account and return it.

        **Grafana shows a token's value exactly once**, in this response. There
        is no endpoint that reads it back, which is why the caller stores it
        before doing anything else and why a rotation deletes the old token
        rather than trying to look it up.

        Any token already on the account is removed first. Not tidiness: a
        rotation whose whole purpose is that the old credential stops working
        would otherwise leave the previous token live and accepted.
        """
        await self._revoke_tokens(client, account_id)
        url = self.endpoint(f"/api/serviceaccounts/{account_id}/tokens")
        response = await send(client, "POST", url, json={"name": SERVICE_ACCOUNT_CREDENTIAL_NAME})
        if response.status_code not in (200, 201):
            raise ServiceDialError(self._failure(response, url))
        payload = response.json()
        key = payload.get("key") if isinstance(payload, dict) else None
        if not isinstance(key, str) or not key:
            raise ServiceDialError(
                ServiceFailure(
                    kind="bad_response",
                    detail=f"{self} issued a service account token with no key in the response",
                    remedy="check the Grafana version and the server's own log",
                )
            )
        return key

    async def _revoke_tokens(self, client: httpx.AsyncClient, account_id: int) -> int:
        """Delete every token on our service account. Returns how many."""
        url = self.endpoint(f"/api/serviceaccounts/{account_id}/tokens")
        response = await send(client, "GET", url)
        if response.status_code != 200:
            raise ServiceDialError(self._failure(response, url))
        payload = response.json()
        tokens = payload if isinstance(payload, list) else []
        removed = 0
        for token in tokens:
            token_id = token.get("id") if isinstance(token, dict) else None
            if not isinstance(token_id, int):
                continue
            deleted = await send(
                client,
                "DELETE",
                self.endpoint(f"/api/serviceaccounts/{account_id}/tokens/{token_id}"),
            )
            if deleted.status_code in (200, 204):
                removed += 1
        return removed


def missing_datasources(existing: Sequence[Mapping[str, Any]]) -> list[str]:
    """Which of `EXPECTED_DATASOURCES` this Grafana does not have.

    By `type` and not by name, because an operator names their datasources
    whatever they like and the platform has no business insisting.
    """
    present = {row.get("type") for row in existing}
    return sorted(key for key, ds_type in EXPECTED_DATASOURCES.items() if ds_type not in present)
