"""The MQTT connection tester (task E5.4a; spec 16.2 row 1, 16.4, 16.5).

Three checks over ONE connection, in an order that is load-bearing:

1. **`connect`** - the credentials and the TLS trust anchor are real. Nothing
   else runs if this fails, because every later failure would be the same
   failure reported three times.
2. **`round_trip`** - publish and subscribe on a reserved leaf under the
   deployment root, proving the account can actually use the namespace the
   control plane will use. A connect alone does not: Mosquitto authenticates
   first and applies the ACL later, so an account with a working password and
   an empty ACL connects happily and then silently does nothing, which is the
   exact shape of a deployment that looks configured and never reconciles.
3. **`dynsec`** - whether the platform can mint per-device credentials here.

**The dynsec verdict is part of the pass/fail, not a warning** (phase-5 fixed
choice 4). dynsec is required for v1: spec 16.4's per-device credentials are
minted through it, and spec 16.5 gates bundle generation on a verified broker,
so a broker where the plugin is `absent` or the account is `denied` cannot be
verified. Reporting that as a warning would let a deployment reach `verified`
and then fail at the one moment it matters - the first device provisioning -
which is the failure this ordering exists to prevent.

**Why the probe runs last, on the same connection.** The phase document
requires that the probe "never publishes to `$CONTROL` on a broker it has not
first confirmed accepts the connection". Running it third on a connection that
has already completed a round trip makes that true by construction rather than
by a check someone has to remember; `dynsec.probe` reinforces it by returning
on a refused SUBACK before its publish line.
"""

import time
from dataclasses import dataclass

from app.contracts.mqtt import deployment_root
from app.services import dynsec
from app.services.clients.mqtt import (
    MqttDialError,
    MqttServiceClient,
    selftest_topic,
)
from app.services.testers.base import (
    CheckResult,
    ServiceCredentials,
    TestResult,
)

#: Longer than the framework default: this tester pays for a TLS handshake, a
#: publish/subscribe round trip and a dynsec round trip in sequence, and a
#: broker across a WAN can spend seconds on the handshake alone. Still well
#: under `WHOLE_CALL_BUDGET_SECONDS`, because the five testers run
#: concurrently and the endpoint's bound is the slowest and not the sum.
MQTT_BUDGET_SECONDS = 18.0


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def client_for(credentials: ServiceCredentials) -> MqttServiceClient:
    """Turn what `resolve_credentials` produced into something dialable.

    Lives on this side of the boundary rather than as a classmethod on the
    client, because `app/services/clients/` must not know about the tester
    framework - `testers/__init__` imports every tester to populate `REGISTRY`,
    so a client importing `ServiceCredentials` closes an import cycle. The
    layering that avoids it is the one E7 wants anyway.

    Raises `ValueError`, `KeyError` or `TypeError` on a credential set missing
    its coordinates. That cannot happen through the API - `MqttSettings`
    requires all four and the `mqtt_coordinates_required` CHECK requires them
    of a stored row - so `run` reports it as a platform defect by exception
    TYPE, and the runner would contain it even if `run` did not.
    """
    if credentials.deployment_id is None or credentials.deployment_slug is None:
        raise ValueError(
            "the mqtt tester needs the deployment's id and slug to build its reserved "
            "topic; resolve_credentials was called without them"
        )
    settings = credentials.settings
    return MqttServiceClient(
        deployment_id=credentials.deployment_id,
        deployment_slug=credentials.deployment_slug,
        host=str(settings["host"]),
        port=int(settings["port"]),
        username=str(settings["username"]),
        password=credentials.secrets.get("password", ""),
        tls_enabled=bool(settings.get("tls_enabled", True)),
        ca_cert_pem=settings.get("ca_cert_pem"),
    )


@dataclass
class MqttTester:
    """Spec 16.2's broker test. Registered as `REGISTRY["mqtt"]`.

    Not frozen, and not by accident: `ServiceTester` declares `service_key`
    and `budget_seconds` as ordinary attributes, and a frozen dataclass makes
    them read-only, which is not the protocol. It holds no state either way -
    every value it needs arrives in `run`'s `credentials`.
    """

    service_key: str = "mqtt"
    budget_seconds: float = MQTT_BUDGET_SECONDS

    async def run(self, credentials: ServiceCredentials) -> TestResult:
        checks: list[CheckResult] = []
        started = time.monotonic()

        try:
            client = client_for(credentials)
        except (ValueError, KeyError, TypeError) as error:
            # A credential set without coordinates cannot reach here through
            # the API - MqttSettings requires all four and the
            # `mqtt_coordinates_required` CHECK requires them of a stored row
            # - so this is a platform defect, reported by TYPE and not by
            # text, following `base.py::_crashed`'s rule.
            return TestResult(
                service_key=self.service_key,
                outcome="fail",
                checks=(
                    CheckResult(
                        name="settings",
                        passed=False,
                        detail=(
                            f"the stored broker settings are incomplete ({type(error).__name__})"
                        ),
                        remedy=(
                            "re-enter the broker host, port, username and password and save "
                            "them, then test again"
                        ),
                        elapsed_ms=_ms(started),
                    ),
                ),
            )

        try:
            async with client.connect() as session:
                checks.append(
                    CheckResult(
                        name="connect",
                        passed=True,
                        detail=f"connected to {client.host}:{client.port} as {client.username}",
                        remedy="",
                        elapsed_ms=_ms(started),
                    )
                )

                round_trip_started = time.monotonic()
                delivered = await client.round_trip(session)
                topic = selftest_topic(client.deployment_slug)
                checks.append(
                    CheckResult(
                        name="round_trip",
                        passed=delivered,
                        detail=(
                            f"published to {topic} and read it back"
                            if delivered
                            else (
                                f"the broker accepted the connection but {topic} did not "
                                "round-trip, so this account cannot use this deployment's "
                                "topic namespace"
                            )
                        ),
                        remedy=(
                            ""
                            if delivered
                            else (
                                "grant this account read and write on the deployment's whole "
                                "topic tree - one ACL entry for "
                                f"'{deployment_root(client.deployment_slug)}/#' is what the "
                                "platform's own account needs; devices get their own, much "
                                "narrower, grants"
                            )
                        ),
                        elapsed_ms=_ms(round_trip_started),
                    )
                )

                probe_started = time.monotonic()
                verdict = await dynsec.probe(session)
                checks.append(
                    CheckResult(
                        name="dynsec",
                        passed=verdict.usable,
                        detail=verdict.detail,
                        remedy=verdict.remedy,
                        elapsed_ms=_ms(probe_started),
                    )
                )
        except MqttDialError as error:
            checks.append(
                CheckResult(
                    name="connect",
                    passed=False,
                    detail=error.failure.detail,
                    remedy=error.failure.remedy,
                    elapsed_ms=_ms(started),
                )
            )

        return TestResult(
            service_key=self.service_key,
            outcome="pass" if all(check.passed for check in checks) else "fail",
            checks=tuple(checks),
        )
