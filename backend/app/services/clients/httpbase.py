"""What the three HTTP-shaped service clients share (tasks E5.4b, E5.4c, E5.4d).

Influx, Prometheus and Grafana are three different APIs reached the same way:
one `httpx.AsyncClient`, one bearer or basic credential, one short timeout, and
a fixed set of ways a dial can fail before the service ever answers. Written
three times, that taxonomy drifts three ways - and the operator-facing text is
the whole product here, so drift is not cosmetic. It lives once, in this
module, and each client adds only the failures that are its own (a database
that does not exist, a remote-write receiver that is switched off).

**No exception string ever reaches operator-facing text.** `base.py::_crashed`
states the rule and names the reason: httpx puts the request URL in its
messages, and a URL can carry a credential in its query string. So every
`detail` here is built from `safe_endpoint`, which strips both the userinfo and
the query, plus the exception TYPE or a known-safe field (`errno`, an SSL
verify message). Where a response body genuinely helps - "database not found"
is the difference between two remedies - it goes through `snippet`, which
truncates it and redacts the caller's own secrets from it first.

**TLS here is the system trust store, deliberately, and unlike the broker.**
D65 pins the broker to `ca_cert_pem` because `deployment_service` carries that
column for it. `InfluxSettings`, `PrometheusSettings` and `GrafanaSettings`
have no CA field (E5.2), so there is nothing to pin to; a private CA on an
HTTPS Influx is an operator's own trust-store problem today. The failure is at
least legible: `tls_trust` says exactly what happened and its remedy says what
would fix it. Adding a CA field to those three models is a schema change and a
wire-format change, and belongs to whoever decides to make it, not to a client
that quietly widens the anchor set instead.

**Nothing here decides a verdict** (`clients/__init__.py`). A `ServiceFailure`
carries the detail and the remedy the tester will show, because the client is
where the knowledge of *what went wrong* actually lives; the tester decides
what that means for the service's outcome.
"""

import socket
import ssl
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

#: The default per-request budget. Every tester's own budget has to cover
#: several of these in sequence, so this is much shorter than
#: `DEFAULT_TESTER_BUDGET_SECONDS`: a service that has not answered a health
#: check in five seconds is not going to answer the next three calls either,
#: and the operator is better served by a fast, specific failure.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: How much of a response body may appear in operator-facing text. Long enough
#: to carry "database not found: telemetry", short enough that a service which
#: echoes a whole request back cannot turn a `detail` into a payload dump.
SNIPPET_LIMIT = 200


@dataclass(frozen=True)
class ServiceFailure:
    """Why a call failed, in terms an operator can act on.

    `kind` is the stable, testable fact - the suite asserts that two failures
    are DISTINGUISHABLE by kind rather than by pinning prose - and `detail`
    and `remedy` are the prose that changes freely as long as the kind does
    not. Same shape as `clients/mqtt.py::ConnectFailure`, on purpose: five
    services, one vocabulary.
    """

    kind: str
    detail: str
    remedy: str


class ServiceDialError(Exception):
    """A call that failed, carrying its classification.

    Raised rather than returned so a client method reads as a normal call and
    the tester's `except ServiceDialError` is the single place a check turns
    into a failing `CheckResult`.
    """

    def __init__(self, failure: ServiceFailure) -> None:
        super().__init__(failure.detail)
        self.failure = failure


def safe_endpoint(url: str) -> str:
    """`https://host:port/path` - scheme, host, port and path, nothing else.

    Both of the things this removes have put credentials in log lines in real
    systems: `https://user:token@host/` carries one in the userinfo, and
    `?u=admin&p=hunter2` carries one in the query. Neither is a shape this
    platform writes, and both are shapes an operator can paste into a URL
    field, which is precisely why the sanitiser is applied to the value the
    operator supplied rather than to the ones the platform builds.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "the configured URL"
    if not parts.scheme and not parts.netloc:
        return url[:SNIPPET_LIMIT]
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", "")) or "the configured URL"


def redact(text: str, secrets: Iterable[str]) -> str:
    """Every one of `secrets` replaced by a marker.

    The last line of defence, not the first: no code path here deliberately
    puts a credential in a message. It exists because a service is free to
    echo whatever it likes in an error body - some proxies echo the
    Authorization header - and a tester's `detail` is rendered straight into
    the S5 wizard.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def snippet(response: httpx.Response, secrets: Iterable[str] = ()) -> str:
    """A truncated, redacted, single-line rendering of a response body."""
    try:
        body = response.text
    except Exception:  # noqa: BLE001  (a body that will not decode is not a crash)
        return ""
    cleaned = " ".join(redact(body, secrets).split())
    if len(cleaned) > SNIPPET_LIMIT:
        cleaned = cleaned[:SNIPPET_LIMIT] + "..."
    return cleaned


def classify_transport_error(error: BaseException, url: str) -> ServiceFailure:
    """Map an httpx transport failure onto one of the named causes.

    Ordered most-specific first, and built on exception TYPE rather than
    message text for the same reason `clients/mqtt.py` is: the type is the
    stable fact. Each branch is a different thing for the operator to go and
    fix, which is what makes `remedy` worth requiring.
    """
    where = safe_endpoint(url)
    if isinstance(error, httpx.UnsupportedProtocol | httpx.InvalidURL):
        return ServiceFailure(
            kind="bad_url",
            detail=f"{where} is not a URL this platform can dial",
            remedy=(
                "enter the full URL including the scheme, for example "
                "'https://influx.example:8181' rather than 'influx.example:8181'"
            ),
        )
    cause = error.__cause__ or error.__context__
    if isinstance(cause, ssl.SSLCertVerificationError):
        return ServiceFailure(
            kind="tls_trust",
            detail=(
                f"{where} presented a certificate this platform does not trust "
                f"({cause.verify_message or cause.reason})"
            ),
            remedy=(
                "use a certificate signed by a CA the platform host trusts, or install the "
                "private CA into that host's trust store; unlike the broker, this service has "
                "no CA field on the services form to pin one against"
            ),
        )
    if isinstance(cause, ssl.SSLError):
        return ServiceFailure(
            kind="tls_handshake",
            detail=f"the TLS handshake with {where} failed ({cause.reason or 'no reason given'})",
            remedy=(
                "confirm the URL's scheme matches the port: an https:// URL against a plaintext "
                "listener, or http:// against a TLS one, fails exactly like this"
            ),
        )
    if isinstance(cause, socket.gaierror):
        return ServiceFailure(
            kind="dns",
            detail=f"the hostname in {where} could not be resolved from the platform host",
            remedy=(
                "check the hostname for a typo, and that it resolves from the platform's "
                "network rather than only from your own machine"
            ),
        )
    if isinstance(cause, ConnectionRefusedError):
        return ServiceFailure(
            kind="unreachable",
            detail=f"nothing accepted a TCP connection at {where}",
            remedy=(
                "check that the service is running and that this is its port; a refused "
                "connection means the host answered and no service is listening there"
            ),
        )
    if isinstance(error, httpx.TimeoutException):
        return ServiceFailure(
            kind="timeout",
            detail=f"{where} did not answer within {DEFAULT_TIMEOUT_SECONDS:g}s",
            remedy=(
                "check that the service is reachable from the platform host and that no "
                "firewall is dropping the connection rather than refusing it"
            ),
        )
    return ServiceFailure(
        kind="unreachable",
        detail=f"the request to {where} failed ({type(error).__name__})",
        remedy=(
            "check the URL, and that this service is reachable from the platform host rather "
            "than only from your own machine"
        ),
    )


def open_client(
    *,
    headers: Mapping[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> httpx.AsyncClient:
    """One short-lived HTTP session for one connection test.

    `follow_redirects=False` on purpose. A redirect from an authenticated
    request is how a credential reaches a third party: httpx would replay the
    Authorization header to wherever the Location pointed. A service that
    answers a connection test with a redirect is misconfigured, and saying so
    is better than following it.
    """
    return httpx.AsyncClient(
        headers=dict(headers or {}),
        auth=auth,
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
    )


async def send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: object,
) -> httpx.Response:
    """One request, with every transport failure already classified.

    The caller deals in status codes and bodies; anything that stopped a
    status code from existing at all arrives as a `ServiceDialError` whose
    failure has a remedy.
    """
    try:
        return await client.request(method, url, **kwargs)  # type: ignore[arg-type]
    except httpx.HTTPError as error:
        raise ServiceDialError(classify_transport_error(error, url)) from error
    except (ssl.SSLError, OSError) as error:  # pragma: no cover - httpx wraps these
        raise ServiceDialError(classify_transport_error(error, url)) from error


def unexpected_status(
    response: httpx.Response, url: str, secrets: Iterable[str] = ()
) -> ServiceFailure:
    """The catch-all for a status no client branch recognises.

    A separate function rather than an inline literal in each client because
    "the service answered something we did not plan for" is the one failure
    every client has, and an operator staring at it needs the status code and
    the body far more than they need our prose.
    """
    body = snippet(response, secrets)
    where = safe_endpoint(url)
    return ServiceFailure(
        kind="unexpected_status",
        detail=f"{where} answered HTTP {response.status_code}" + (f": {body}" if body else ""),
        remedy=(
            "check that this URL is the service's own API endpoint and not a reverse proxy, "
            "load balancer or single-sign-on page in front of it; the status above is what "
            "the platform received"
        ),
    )
