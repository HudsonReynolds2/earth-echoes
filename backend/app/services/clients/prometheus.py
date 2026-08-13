"""Dialling a deployment's Prometheus (task E5.4c; spec 16.2 row 3).

**Two endpoints with two roles, and telling them apart is the whole unit.** The
platform QUERIES the read URL; the Aggregators' agents PUSH to the remote-write
URL. They are frequently the same host and frequently not, they can fail
independently, and the failure an operator actually hits is the second one
working exactly like the first right up until no data ever arrives.

The remote-write receiver is **off by default** in Prometheus. Running without
`--web.enable-remote-write-receiver`, `/api/v1/write` is simply not routed, and
Prometheus answers **404**. With the flag and bad credentials it answers 401.
With the flag and good credentials it answers 204. Three states, three
remedies, and only the middle one is about the password - which is why the
acceptance criterion insists they stay distinguishable and why a boolean
"remote write ok" would be useless.

**The probe sends a well-formed but EMPTY write request.** A snappy-compressed
protobuf `WriteRequest` with zero timeseries is a legal remote-write payload
that Prometheus accepts and stores nothing from. That answers the question the
probe is asking - is the receiver enabled and are these credentials accepted -
without putting a fabricated series into an operator's monitoring data, which
is a thing a connection test has no business doing. `snappy` is not a
dependency: the empty message is zero bytes, and snappy's framing of an empty
payload is itself empty, so the body is `b""` and there is nothing to compress.
"""

from collections.abc import Iterator
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

#: What `remote_write_probe` concluded. `accepted` is the only pass.
RemoteWriteVerdict = str


@dataclass(frozen=True)
class PrometheusClient:
    """One deployment's Prometheus, dialable.

    `remote_write_password` is `repr=False` behind a `__str__` naming only the
    endpoints, the `BrokerCoordinates`/D66 precedent every client in this
    package follows.
    """

    read_url: str
    remote_write_url: str
    remote_write_user: str = ""
    remote_write_password: str = field(repr=False, default="")
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __str__(self) -> str:
        return f"prometheus {safe_endpoint(self.read_url)}"

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.remote_write_password,) if self.remote_write_password else ()

    @property
    def auth(self) -> tuple[str, str] | None:
        """Basic auth, or None when the operator gave no credentials.

        Prometheus's own `web_config.yml` uses basic auth, so this is the shape
        spec 16.2 means; an unauthenticated Prometheus is a legitimate
        deployment and passing `("", "")` to it would send an empty
        Authorization header rather than none.
        """
        if not self.remote_write_user and not self.remote_write_password:
            return None
        return (self.remote_write_user, self.remote_write_password)

    @asynccontextmanager
    async def session(self) -> Any:
        async with open_client(auth=self.auth, timeout=self.timeout) as client:
            yield client

    async def instant_query(self, client: httpx.AsyncClient, promql: str) -> list[dict[str, Any]]:
        """One instant query against the READ url. Raises `ServiceDialError`.

        A 200 whose `status` field is not `success` is a failure even though
        the HTTP layer succeeded: Prometheus reports a bad query that way, and
        treating it as a pass would make a broken read URL look healthy.
        """
        url = f"{self.read_url.rstrip('/')}/api/v1/query"
        response = await send(client, "GET", url, params={"query": promql})
        if response.status_code in (401, 403):
            raise ServiceDialError(
                ServiceFailure(
                    kind="auth",
                    detail=(
                        f"{safe_endpoint(url)} rejected these credentials "
                        f"(HTTP {response.status_code})"
                    ),
                    remedy=(
                        "check the username and password. Prometheus's basic auth lives in its "
                        "--web.config.file; the password there is a bcrypt hash of the value "
                        "entered here"
                    ),
                )
            )
        if response.status_code != 200:
            raise ServiceDialError(unexpected_status(response, url, self.secrets))
        try:
            payload = response.json()
        except ValueError as error:
            raise ServiceDialError(
                ServiceFailure(
                    kind="bad_response",
                    detail=f"{safe_endpoint(url)} answered 200 with a body that is not JSON",
                    remedy=(
                        "check that this URL reaches Prometheus directly rather than a proxy or "
                        "sign-in page that answers 200 for everything"
                    ),
                )
            ) from error
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ServiceDialError(
                ServiceFailure(
                    kind="bad_response",
                    detail=(
                        f"{safe_endpoint(url)} answered 200 but not with a successful query "
                        f"result ({snippet(response, self.secrets)})"
                    ),
                    remedy=(
                        "check that this URL is Prometheus's own HTTP API root, without an "
                        "/api/v1 suffix; the platform appends the API path itself"
                    ),
                )
            )
        result = payload.get("data", {})
        rows = result.get("result") if isinstance(result, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    async def remote_write_probe(
        self, client: httpx.AsyncClient
    ) -> tuple[RemoteWriteVerdict, ServiceFailure | None]:
        """Is the remote-write receiver enabled, and are these credentials good?

        Returns `("accepted", None)`, or a verdict plus the failure to report.
        Not raised, because all three outcomes are answers to the question
        rather than errors in asking it - and the two failing ones have
        completely different remedies.
        """
        url = self.remote_write_url
        response = await send(
            client,
            "POST",
            url,
            content=b"",
            headers={
                "Content-Type": "application/x-protobuf",
                "Content-Encoding": "snappy",
                "X-Prometheus-Remote-Write-Version": "0.1.0",
            },
        )
        if response.status_code in (200, 202, 204):
            return "accepted", None
        if response.status_code in (401, 403):
            return "unauthorized", ServiceFailure(
                kind="auth",
                detail=(
                    f"the remote-write endpoint {safe_endpoint(url)} rejected these credentials "
                    f"(HTTP {response.status_code})"
                ),
                remedy=(
                    "check the remote-write username and password. This is the credential the "
                    "Aggregators' agents will push metrics with, so it is checked here rather "
                    "than discovered when a Pod goes quiet"
                ),
            )
        if response.status_code == 404:
            return "receiver_disabled", ServiceFailure(
                kind="receiver_disabled",
                detail=(
                    f"{safe_endpoint(url)} answered HTTP 404, so Prometheus is running without "
                    "its remote-write receiver"
                ),
                remedy=(
                    "start Prometheus with --web.enable-remote-write-receiver. The receiver is "
                    "OFF by default, and without it the Aggregators' agents have nowhere to "
                    "push metrics"
                ),
            )
        if response.status_code == 405:
            return "receiver_disabled", ServiceFailure(
                kind="receiver_disabled",
                detail=(
                    f"{safe_endpoint(url)} answered HTTP 405, so this URL exists but does not "
                    "accept a remote-write POST"
                ),
                remedy=(
                    "check that this is the remote-write path (usually /api/v1/write) and that "
                    "Prometheus was started with --web.enable-remote-write-receiver"
                ),
            )
        return "unexpected", unexpected_status(response, url, self.secrets)


def series_names(rows: Iterator[dict[str, Any]] | list[dict[str, Any]]) -> list[str]:
    """The `__name__` label of each returned series, for a readable detail."""
    out: list[str] = []
    for row in rows:
        metric = row.get("metric")
        if isinstance(metric, dict) and isinstance(metric.get("__name__"), str):
            out.append(metric["__name__"])
    return out
