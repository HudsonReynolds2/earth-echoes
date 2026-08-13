"""Dialling one deployment's MQTT broker for a connection test (task E5.4a).

The client E5.4a's tester is written against, and the one E5.6 will mint
credentials over. It is deliberately thin: connect, publish, subscribe, and a
classification of why a connect failed. **No verdicts** - the tester owns those
(see `app/services/clients/__init__.py`).

**TLS is `broker.py::tls_context` and never a second rule.** D65 makes a pinned
`ca_cert_pem` the ONLY trust anchor rather than "the system store plus this
one", because widening the anchor set would mean any public CA's certificate
for the broker's hostname also verified - strictly weaker than the check the
stored PEM was put there to make. A tester that grew its own SSL context would
be testing a connection the control plane will never make. So this module
builds a `BrokerCoordinates` and calls that function, which is also why a
candidate's coordinates are proved dialable in exactly the shape
`MqttClientManager` will later dial them in.

**Why the connect error taxonomy is built on `__cause__` and not on message
text.** aiomqtt wraps everything below it in `MqttError`, whose `str` is the
underlying exception's - so the type of the cause is the stable fact and the
string is not. Measured against a real broker:

| what the operator did wrong  | exception            | cause                     |
| ---------------------------- | -------------------- | ------------------------- |
| wrong password or username   | `MqttConnectError`   | -                         |
| wrong or missing CA          | `MqttError`          | `ssl.SSLCertVerificationError` |
| host up, nothing listening   | `MqttError`          | `ConnectionRefusedError`  |
| hostname does not resolve    | `MqttError`          | `socket.gaierror`         |
| TLS off against a TLS listener | `MqttError`        | `TimeoutError`            |

Each of those is a different thing for the operator to go fix, which is the
whole reason E5.3 made `remedy` mandatory.

**On putting exception text in operator-facing detail.** `base.py::_crashed`
refuses to, and is right to: an UNEXPECTED exception could be anything,
including an httpx error carrying a URL with a token in its query string. Here
the branches are recognised and their text is known to be credential-free -
`ssl`'s verification reasons, `errno` strings, and Mosquitto's CONNACK
description. The unrecognised branch falls back to the type name, keeping
`_crashed`'s rule exactly where its reasoning applies.

**This module does not import from `app/services/testers/`, and that is
enforced by a circular import rather than by discipline.** `testers/__init__`
imports every tester so `REGISTRY` is populated, and every tester imports its
client - so a client that reached back for `ServiceCredentials` would close the
loop and fail at import. Which is the right answer anyway: clients dial, and
knowing nothing about verdicts or credential resolution is what makes them
reusable by E7. Turning a `ServiceCredentials` into one of these is the
tester's job (`testers/mqtt.py::client_for`).
"""

import asyncio
import contextlib
import socket
import ssl
import uuid as uuid_module
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import aiomqtt
from aiomqtt.exceptions import MqttConnectError

from app.contracts.mqtt import QOS, deployment_root
from app.controlplane.broker import BrokerCoordinates, tls_context

#: The leaf the round trip uses, under the deployment root and therefore
#: inside the one grant the platform account holds (`topic readwrite
#: eoe/{slug}/#`, `devbroker.acl_file_text`). Built through `deployment_root`
#: and never written out as a literal, so a change to the root moves this with
#: it - and so a broker whose ACL is cut correctly for the platform passes
#: without needing a widened grant. `_selftest` cannot collide with a real
#: topic: every other topic under the root goes through `agg/{uuid}`.
SELFTEST_LEAF = "_selftest"

#: Seconds to wait on the socket and on the round trip. Well under the MQTT
#: tester's own budget, which has the dynsec probe to pay for as well.
DEFAULT_CONNECT_TIMEOUT = 6.0
DEFAULT_ROUND_TRIP_TIMEOUT = 5.0


def selftest_topic(deployment_slug: str) -> str:
    """`eoe/{slug}/_selftest` - the reserved leaf the connection test uses."""
    return f"{deployment_root(deployment_slug)}/{SELFTEST_LEAF}"


@dataclass(frozen=True)
class ConnectFailure:
    """Why a dial failed, in terms an operator can act on.

    `kind` is for the tester's `CheckResult.name` and for tests that assert
    two failures are DISTINGUISHABLE without pinning prose.
    """

    kind: str
    detail: str
    remedy: str


def _ssl_reason(error: ssl.SSLError) -> str:
    """The most specific thing `ssl` can tell an operator about a handshake.

    `verify_message` ("self-signed certificate in certificate chain") is the
    useful one and exists only on a verification failure raised by the stdlib;
    `reason` is the symbolic code. Both are read defensively because this
    string is on the operator's screen, and a classifier that raises while
    explaining a failure turns one bad certificate into a platform defect.
    """
    for attribute in ("verify_message", "reason"):
        value = getattr(error, attribute, None)
        if value:
            return str(value)
    return type(error).__name__


def classify_connect_error(error: BaseException, host: str, port: int) -> ConnectFailure:
    """Map an aiomqtt connect failure onto one of the named causes.

    Ordered most-specific first. Every branch names the host and port, which
    is the operator's own configuration read back to them, and none of them
    can name a credential.
    """
    where = f"{host}:{port}"
    if isinstance(error, MqttConnectError):
        return ConnectFailure(
            kind="authentication",
            detail=f"the broker at {where} refused these credentials ({error})",
            remedy=(
                "check the username and password for this broker; if the account is new, "
                "confirm it exists in the broker's password file or its dynamic security "
                "configuration and that the broker has reloaded"
            ),
        )
    cause = error.__cause__ or error.__context__
    if isinstance(cause, ssl.SSLCertVerificationError):
        return ConnectFailure(
            kind="tls_trust",
            detail=(
                f"the broker at {where} presented a certificate this platform does not "
                f"trust ({_ssl_reason(cause)})"
            ),
            remedy=(
                "paste the broker's CA certificate into the CA certificate field. The "
                "platform trusts THAT certificate and nothing else for this broker, so a "
                "self-signed or private CA works and an empty field means the public trust "
                "store is used instead"
            ),
        )
    if isinstance(cause, ssl.SSLError):
        return ConnectFailure(
            kind="tls_handshake",
            detail=f"the TLS handshake with {where} failed ({_ssl_reason(cause)})",
            remedy=(
                "confirm this port is the broker's TLS listener and that it offers TLS 1.2 "
                "or later; a plaintext listener will not complete this handshake"
            ),
        )
    if isinstance(cause, socket.gaierror):
        return ConnectFailure(
            kind="dns",
            detail=f"the hostname in {where} could not be resolved from the platform host",
            remedy=(
                "check the hostname for a typo, and that it resolves from the platform's "
                "network rather than only from your own machine"
            ),
        )
    if isinstance(cause, ConnectionRefusedError):
        return ConnectFailure(
            kind="unreachable",
            detail=f"nothing accepted a TCP connection on {where}",
            remedy=(
                "check that the broker is running and that this is its port; a refused "
                "connection means the host answered and no service is listening there"
            ),
        )
    if isinstance(cause, TimeoutError | OSError):
        return ConnectFailure(
            kind="unreachable",
            detail=f"the connection to {where} timed out or was dropped",
            remedy=(
                f"check that {where} is reachable from the platform host and that no "
                "firewall is dropping the connection; if TLS is disabled here but the "
                "broker requires it, the handshake will hang exactly like this"
            ),
        )
    return ConnectFailure(
        kind="unreachable",
        detail=f"the connection to {where} failed ({type(error).__name__})",
        remedy=(
            "check the host, port and TLS setting for this broker, and that it is reachable "
            "from the platform host"
        ),
    )


class MqttDialError(Exception):
    """A connect that failed, carrying its classification.

    Raised rather than returned so `async with client.connect()` reads like
    every other connection in the codebase; the tester catches it and turns
    `failure` into its `CheckResult`.
    """

    def __init__(self, failure: ConnectFailure) -> None:
        super().__init__(failure.detail)
        self.failure = failure


@dataclass(frozen=True)
class MqttServiceClient:
    """One deployment's broker, dialable.

    `password` is `repr=False` and `__str__` names only the deployment and the
    socket, exactly as `BrokerCoordinates` does (D66) - so a stray `%r` in a
    later tester cannot put a broker password in a log line. Do not remove
    either.
    """

    deployment_id: uuid_module.UUID
    deployment_slug: str
    host: str
    port: int
    username: str
    password: str = field(repr=False, default="")
    tls_enabled: bool = True
    ca_cert_pem: str | None = None

    def __str__(self) -> str:
        return f"{self.deployment_slug} broker at {self.host}:{self.port}"

    def coordinates(self) -> BrokerCoordinates:
        """The same value object the control plane dials with.

        Built rather than loaded, because a connection test runs against
        credentials that may never have been stored (spec 16.2: "validates
        each entry with a live connection test before accepting it"). Its only
        job here is to feed `tls_context`, so D65's pinned-anchor rule holds
        identically for a candidate and for a saved row.
        """
        return BrokerCoordinates(
            deployment_id=self.deployment_id,
            slug=self.deployment_slug,
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            tls_enabled=self.tls_enabled,
            ca_cert_pem=self.ca_cert_pem,
        )

    @contextlib.asynccontextmanager
    async def connect(
        self, *, timeout: float = DEFAULT_CONNECT_TIMEOUT
    ) -> AsyncIterator[aiomqtt.Client]:
        """Open one short-lived session, or raise `MqttDialError`.

        `clean_session=True` and a per-call random identifier: a test must not
        inherit a queued backlog from a previous run, and two operators
        clicking Test at the same moment must not evict each other's session,
        which is what a fixed client id would do.
        """
        client = aiomqtt.Client(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            identifier=f"eoe-servicetest-{uuid_module.uuid4().hex[:12]}",
            tls_context=tls_context(self.coordinates()),
            timeout=timeout,
            clean_session=True,
        )
        try:
            await client.__aenter__()
        except Exception as error:
            raise MqttDialError(classify_connect_error(error, self.host, self.port)) from error
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)

    async def round_trip(
        self,
        client: aiomqtt.Client,
        *,
        timeout: float = DEFAULT_ROUND_TRIP_TIMEOUT,
        payload: bytes | None = None,
    ) -> bool:
        """Subscribe to the reserved leaf, publish to it, and read it back.

        Returns False on a refused subscription or a message that never
        arrives; both mean the same thing to the operator (this account cannot
        actually use this deployment's namespace) and the tester says so with
        one remedy.

        Not retained, and QoS 1: a retained self-test message would sit on the
        broker forever and be delivered to every device that subscribed to the
        deployment root afterwards.
        """
        topic = selftest_topic(self.deployment_slug)
        body = payload if payload is not None else uuid_module.uuid4().hex.encode()
        try:
            codes = await client.subscribe(topic, qos=QOS)
        except aiomqtt.MqttError:
            return False
        if any(getattr(code, "is_failure", False) for code in codes):
            return False
        await client.publish(topic, body, qos=QOS, retain=False)
        try:
            async with asyncio.timeout(timeout):
                async for message in client.messages:
                    if message.topic.matches(topic) and bytes(message.payload) == body:
                        return True
        except TimeoutError:
            return False
        return False
