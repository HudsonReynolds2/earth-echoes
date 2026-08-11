"""E5.4a: the MQTT tester and the dynsec probe, against real brokers.

Two containers for the whole module, because the acceptance criteria are about
what real Mosquitto does and a fake cannot have the property under test. The
dev broker (`acl_file`, no plugin) produces the `absent` verdict and every
connect failure; the dynsec broker produces `available` and `denied` from two
accounts in one `dynamic-security.json`, since which of those two an operator
gets is a property of their account and not of their broker.

**The probe's three verdicts were established by experiment before this suite
was written**, and the reason is worth keeping: the intuitive discriminator
does not work. A Mosquitto using `acl_file` GRANTS a subscription to a topic
its file never mentions, then silently refuses the matching publish - so "the
publish was refused" cannot separate a missing plugin from an unprivileged
account. Dynsec refuses the SUBSCRIBE. The SUBACK is therefore the thing that
carries the information, and `test_the_three_verdicts_are_distinguishable`
pins all three against real brokers so a later refactor cannot quietly
collapse two of them.
"""

import asyncio
import socket
import ssl
import uuid

import aiomqtt
import pytest
from aiomqtt.exceptions import MqttConnectError
from conftest import (
    DYNSEC_ADMIN_PASSWORD,
    DYNSEC_ADMIN_USER,
    DYNSEC_PLAIN_PASSWORD,
    DYNSEC_PLAIN_USER,
    dynsec_broker,
    ephemeral_broker,
    free_port,
)

from app.contracts.mqtt import QOS, deployment_root
from app.devbroker import Account, generate_tls_material, write_artifacts
from app.services import dynsec
from app.services.clients.mqtt import (
    MqttDialError,
    MqttServiceClient,
    classify_connect_error,
    selftest_topic,
)
from app.services.testers import REGISTRY
from app.services.testers.base import ServiceCredentials, resolve_credentials
from app.services.testers.mqtt import MqttTester, client_for

pytestmark = pytest.mark.anyio

SLUG = "e54a-services"
PLATFORM_USER = f"platform-{SLUG}"
PLATFORM_PASSWORD = "platform-account-password"
DEPLOYMENT_ID = uuid.UUID("5e54a000-0000-4000-8000-00000000000a")


# --- Brokers ----------------------------------------------------------------


@pytest.fixture(scope="module")
def dev_broker(tmp_path_factory):
    """The committed dev broker: `acl_file`, no plugin, one platform account
    cut to `eoe/{slug}/#` exactly as `devbroker.acl_file_text` cuts it."""
    out = tmp_path_factory.mktemp("e54a-dev-certs")
    account = Account(
        username=PLATFORM_USER,
        password=PLATFORM_PASSWORD,
        kind="platform",
        deployment_slug=SLUG,
    )
    write_artifacts(out, [account], host="127.0.0.1", port=8883, regenerate_tls=True)
    with ephemeral_broker(out) as broker:
        yield broker


@pytest.fixture(scope="module")
def dynsec_enabled_broker(tmp_path_factory):
    """The same container with the dynamic security plugin loaded instead."""
    with dynsec_broker(tmp_path_factory.mktemp("e54a-dynsec"), SLUG) as broker:
        yield broker


def _ca(broker) -> str:
    return (broker.dev_dir / "ca.crt").read_text(encoding="utf-8")


def _client(broker, username: str, password: str, **overrides) -> MqttServiceClient:
    kwargs = {
        "deployment_id": DEPLOYMENT_ID,
        "deployment_slug": SLUG,
        "host": "127.0.0.1",
        "port": broker.port,
        "username": username,
        "password": password,
        "tls_enabled": True,
        "ca_cert_pem": _ca(broker),
    }
    kwargs.update(overrides)
    return MqttServiceClient(**kwargs)


def _credentials(broker, username: str, password: str, **settings) -> ServiceCredentials:
    base = {
        "host": "127.0.0.1",
        "port": broker.port,
        "tls_enabled": True,
        "ca_cert_pem": _ca(broker),
        "username": username,
    }
    base.update(settings)
    return ServiceCredentials(
        service_key="mqtt",
        settings=base,
        secrets={"password": password},
        deployment_id=DEPLOYMENT_ID,
        deployment_slug=SLUG,
    )


def _check(result, name):
    found = [check for check in result.checks if check.name == name]
    assert found, f"no {name!r} check in {[c.name for c in result.checks]}"
    return found[0]


# --- The happy path, and what the dev broker can and cannot do --------------


async def test_the_round_trip_succeeds_against_a_real_dev_broker(dev_broker):
    """First acceptance clause: the round trip works, on the ACL the platform
    account actually has. Asserted through the client rather than the tester so
    a dynsec failure cannot mask a working round trip."""
    client = _client(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD)
    async with client.connect() as session:
        assert await client.round_trip(session) is True


async def test_the_reserved_leaf_is_built_from_the_topic_builder():
    """ "Built through `contracts.mqtt.deployment_root` and never a literal"
    (phase document). Asserted against the builder, so a change to the root
    moves the tester with it instead of silently landing outside the ACL."""
    assert selftest_topic(SLUG).startswith(deployment_root(SLUG) + "/")
    assert selftest_topic(SLUG) == f"{deployment_root(SLUG)}/_selftest"


async def test_the_self_test_message_is_not_retained(dev_broker):
    """A retained self-test would sit on the broker forever and be delivered
    to every device that later subscribed to the deployment root. Asserted by
    subscribing AFTER the round trip and seeing nothing."""
    client = _client(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD)
    async with client.connect() as session:
        assert await client.round_trip(session) is True

    async with client.connect() as later:
        await later.subscribe(selftest_topic(SLUG), qos=QOS)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(2.0):
                async for _ in later.messages:
                    break


# --- Connect failures, distinguishable one from another ---------------------


async def test_a_wrong_password_fails_with_an_authentication_reason(dev_broker):
    client = _client(dev_broker, PLATFORM_USER, "not-the-password")
    with pytest.raises(MqttDialError) as raised:
        async with client.connect():
            pass
    assert raised.value.failure.kind == "authentication"
    assert raised.value.failure.remedy


async def test_an_untrusted_ca_fails_with_a_trust_reason(dev_broker):
    """A different CA, not a missing one: the platform must reject a broker
    whose certificate is signed by anything other than the pinned anchor
    (D65), which is the property a "system store plus this one" context would
    quietly lose."""
    other_ca = generate_tls_material()["ca.crt"].decode()
    client = _client(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD, ca_cert_pem=other_ca)
    with pytest.raises(MqttDialError) as raised:
        async with client.connect():
            pass
    assert raised.value.failure.kind == "tls_trust"
    assert raised.value.failure.remedy


async def test_an_unreachable_host_fails_with_a_reachability_reason(dev_broker):
    """A port nothing is listening on, claimed through `free_port` so no
    concurrent gate run can start something there mid-test."""
    client = _client(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD, port=free_port())
    with pytest.raises(MqttDialError) as raised:
        async with client.connect():
            pass
    assert raised.value.failure.kind == "unreachable"
    assert raised.value.failure.remedy


async def test_the_three_named_failures_are_mutually_distinguishable(dev_broker):
    """The acceptance clause reads "fails with a DISTINGUISHABLE reason for
    each of wrong password, untrusted CA and unreachable host". Asserted as
    three distinct kinds, three distinct details and three distinct remedies,
    rather than three tests that each pass on their own while two of them say
    the same thing."""
    other_ca = generate_tls_material()["ca.crt"].decode()
    cases = {
        "wrong password": _client(dev_broker, PLATFORM_USER, "not-the-password"),
        "untrusted CA": _client(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD, ca_cert_pem=other_ca),
        "unreachable host": _client(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD, port=free_port()),
    }
    failures = {}
    for label, client in cases.items():
        with pytest.raises(MqttDialError) as raised:
            async with client.connect():
                pass
        failures[label] = raised.value.failure

    assert len({f.kind for f in failures.values()}) == 3
    assert len({f.detail for f in failures.values()}) == 3
    assert len({f.remedy for f in failures.values()}) == 3
    assert all(f.remedy for f in failures.values())


def test_no_connect_failure_can_name_a_credential():
    """Every branch of the classifier, checked for the two things it must
    never contain. `classify_connect_error` names a host and a port because
    that is the operator's own configuration read back to them; a password is
    a different matter, and the unrecognised branch falls back to the
    exception TYPE for exactly this reason (`base.py::_crashed`'s rule)."""
    secret = "s3cr3t-broker-password"
    errors: list[BaseException] = [
        MqttConnectError(135),
        _with_cause(aiomqtt.MqttError("x"), ssl.SSLCertVerificationError("bad")),
        _with_cause(aiomqtt.MqttError("x"), ssl.SSLError("handshake")),
        _with_cause(aiomqtt.MqttError("x"), ConnectionRefusedError(111, "Connection refused")),
        _with_cause(aiomqtt.MqttError("x"), socket.gaierror(-2, "no name")),
        _with_cause(aiomqtt.MqttError("x"), TimeoutError()),
        _with_cause(aiomqtt.MqttError(secret), RuntimeError(secret)),
    ]
    for error in errors:
        failure = classify_connect_error(error, "broker.example", 8883)
        assert secret not in failure.detail
        assert secret not in failure.remedy
        assert failure.remedy


def _with_cause(error: BaseException, cause: BaseException) -> BaseException:
    error.__cause__ = cause
    return error


# --- The three dynsec verdicts ----------------------------------------------


async def test_the_probe_reports_absent_against_the_dev_broker(dev_broker):
    """The current dev broker loads no plugin, and the probe must say so
    rather than reporting the account's lack of privilege - which is what a
    verdict derived from the refused PUBLISH would say instead."""
    client = _client(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD)
    async with client.connect() as session:
        verdict = await dynsec.probe(session)
    assert verdict.verdict == "absent"
    assert verdict.usable is False
    assert "plugin" in verdict.remedy


async def test_the_probe_reports_available_for_a_dynsec_administrator(dynsec_enabled_broker):
    client = _client(dynsec_enabled_broker, DYNSEC_ADMIN_USER, DYNSEC_ADMIN_PASSWORD)
    async with client.connect() as session:
        verdict = await dynsec.probe(session)
    assert verdict.verdict == "available"
    assert verdict.usable is True
    assert verdict.remedy == ""


async def test_the_probe_reports_denied_for_an_account_without_the_admin_role(
    dynsec_enabled_broker,
):
    """The plain account holds the deployment's whole topic tree - the same
    cut the platform account gets on the dev broker - and lacks only the
    plugin's `admin` role. So a `denied` verdict here is caused by the missing
    role and not by an account that cannot do anything at all."""
    client = _client(dynsec_enabled_broker, DYNSEC_PLAIN_USER, DYNSEC_PLAIN_PASSWORD)
    async with client.connect() as session:
        verdict = await dynsec.probe(session)
    assert verdict.verdict == "denied"
    assert verdict.usable is False
    assert "admin" in verdict.remedy


async def test_the_three_verdicts_are_distinguishable(dev_broker, dynsec_enabled_broker):
    """All three at once, with distinct details and distinct remedies. A
    boolean probe would collapse `absent` and `denied`, whose remedies are
    "enable the plugin" and "grant this account the admin role" - different
    people doing different things (phase-5 fixed choice 4)."""
    verdicts = {}
    for label, broker, user, password in (
        ("absent", dev_broker, PLATFORM_USER, PLATFORM_PASSWORD),
        ("available", dynsec_enabled_broker, DYNSEC_ADMIN_USER, DYNSEC_ADMIN_PASSWORD),
        ("denied", dynsec_enabled_broker, DYNSEC_PLAIN_USER, DYNSEC_PLAIN_PASSWORD),
    ):
        client = _client(broker, user, password)
        async with client.connect() as session:
            verdicts[label] = await dynsec.probe(session)

    assert {label: v.verdict for label, v in verdicts.items()} == {
        "absent": "absent",
        "available": "available",
        "denied": "denied",
    }
    assert len({v.detail for v in verdicts.values()}) == 3
    assert verdicts["absent"].remedy != verdicts["denied"].remedy


async def test_the_probe_never_publishes_to_control_without_authorisation(
    dynsec_enabled_broker,
):
    """ "The probe never publishes to `$CONTROL` on a broker it has not first
    confirmed accepts the connection" (phase document, E5.4a).

    **Asserted from the broker's log, and the reason is a finding worth
    keeping.** The obvious instrument - a second administrator subscribed to
    `$CONTROL/dynamic-security/v1`, watching - does not work: Mosquitto's
    dynamic security plugin HANDLES a control publish internally and never
    distributes it, so no subscriber sees one no matter who published it. That
    was measured, not assumed; a watcher-based version of this test failed
    against the authorized case. The broker's own log at `log_type all` is the
    only witness that exists.

    **Paired with the positive case in the same test**, because an empty log
    slice is also what a broken assertion looks like - the same reason E5.6's
    acceptance pairs its denial assertion with an authorized publish.
    """

    quoted_topic = f"'{dynsec.CONTROL_TOPIC}'"

    async def probe_as(user: str, password: str) -> list[str]:
        """The broker's log lines recording a $CONTROL publish, for one probe.

        Matches both "Received PUBLISH" and "Denied PUBLISH", so a probe that
        published and was refused still counts as having published - which is
        exactly the thing under test.
        """
        before = len(dynsec_enabled_broker.logs())
        probed = _client(dynsec_enabled_broker, user, password)
        async with probed.connect() as session:
            await dynsec.probe(session)
        # The log is written as the broker processes; give it the moment
        # between our publish returning and the line reaching stdout.
        await asyncio.sleep(0.5)
        appeared = dynsec_enabled_broker.logs()[before:]
        return [
            line for line in appeared.splitlines() if "PUBLISH" in line and quoted_topic in line
        ]

    authorised = await probe_as(DYNSEC_ADMIN_USER, DYNSEC_ADMIN_PASSWORD)
    assert authorised, (
        "the broker logged no $CONTROL publish for an AUTHORIZED probe, so the absence of "
        "one in the unauthorized case below would prove nothing about the probe"
    )

    refused = await probe_as(DYNSEC_PLAIN_USER, DYNSEC_PLAIN_PASSWORD)
    assert refused == [], (
        "the probe published to $CONTROL on a broker that had just refused it the control "
        f"topic: {refused}"
    )


async def test_the_probe_returns_before_publishing_when_the_subscribe_is_refused():
    """The same property as the test above, at the level of control flow and
    without a container: a refused SUBACK must return `denied` having
    published nothing at all.

    Both exist on purpose. The container test proves it against the broker
    that actually refuses; this one is decisive about WHY - it would still
    fail if the probe published and the broker happened to drop it.
    """

    class RecordingClient:
        """The two methods `dynsec.probe` uses, and a record of the calls."""

        def __init__(self, granted: bool) -> None:
            self.granted = granted
            self.published: list[str] = []

        async def subscribe(self, topic, qos=None):  # noqa: ANN001, ARG002
            return [_FakeReasonCode(is_failure=not self.granted)]

        async def publish(self, topic, payload=None, qos=None, retain=False):  # noqa: ANN001, ARG002
            self.published.append(topic)

        @property
        def messages(self):
            raise AssertionError("the refused path must not wait for a response")

    refused = RecordingClient(granted=False)
    verdict = await dynsec.probe(refused)  # type: ignore[arg-type]
    assert verdict.verdict == "denied"
    assert refused.published == []


class _FakeReasonCode:
    def __init__(self, is_failure: bool) -> None:
        self.is_failure = is_failure


# --- The tester, and fixed choice 4 -----------------------------------------


async def test_a_broker_without_dynsec_fails_the_tester(dev_broker):
    """Fixed choice 4, built rather than written: dynsec is required for v1,
    so an `absent` verdict is a FAILURE and not a warning - even though the
    connection and the round trip both worked. A deployment allowed to reach
    `verified` here would fail at the first device provisioning instead, which
    is the whole reason the verdict is part of the pass."""
    result = await MqttTester().run(_credentials(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD))
    assert result.outcome == "fail"
    assert _check(result, "connect").passed is True
    assert _check(result, "round_trip").passed is True
    assert _check(result, "dynsec").passed is False


async def test_a_dynsec_broker_with_an_admin_account_passes(dynsec_enabled_broker):
    """The only configuration that can reach `verified` under fixed choice 4."""
    result = await MqttTester().run(
        _credentials(dynsec_enabled_broker, DYNSEC_ADMIN_USER, DYNSEC_ADMIN_PASSWORD)
    )
    assert result.outcome == "pass"
    assert [check.name for check in result.checks] == ["connect", "round_trip", "dynsec"]
    assert all(check.passed for check in result.checks)


async def test_a_denied_account_fails_the_tester(dynsec_enabled_broker):
    result = await MqttTester().run(
        _credentials(dynsec_enabled_broker, DYNSEC_PLAIN_USER, DYNSEC_PLAIN_PASSWORD)
    )
    assert result.outcome == "fail"
    assert _check(result, "round_trip").passed is True
    assert _check(result, "dynsec").passed is False


async def test_a_refused_connection_reports_only_the_connect_check(dev_broker):
    """Nothing runs after a failed connect, because every later check would
    report the same failure a second and third time and bury the one thing
    the operator has to fix."""
    result = await MqttTester().run(_credentials(dev_broker, PLATFORM_USER, "wrong"))
    assert result.outcome == "fail"
    assert [check.name for check in result.checks] == ["connect"]
    assert result.checks[0].remedy


async def test_every_failing_check_carries_a_remedy(dev_broker, dynsec_enabled_broker):
    """E5.3's rule, applied to this tester across every failure path it has.
    A failing check with an empty remedy is a defect the suite fails on."""
    results = [
        await MqttTester().run(_credentials(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD)),
        await MqttTester().run(_credentials(dev_broker, PLATFORM_USER, "wrong")),
        await MqttTester().run(
            _credentials(dynsec_enabled_broker, DYNSEC_PLAIN_USER, DYNSEC_PLAIN_PASSWORD)
        ),
        await MqttTester().run(
            _credentials(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD, port=free_port())
        ),
    ]
    failing = [check for result in results for check in result.checks if not check.passed]
    assert failing, "this table is meant to exercise failures"
    for check in failing:
        assert check.remedy.strip(), f"{check.name} failed with no remedy"


async def test_no_result_or_log_carries_the_password(dev_broker, dynsec_enabled_broker, caplog):
    """Definition of done: no service credential in any API response or log
    line. Asserted by scanning for the literal value rather than by inspecting
    field names, which is what E5.2's own secret test does."""
    caplog.set_level(0)
    results = [
        await MqttTester().run(_credentials(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD)),
        await MqttTester().run(_credentials(dev_broker, PLATFORM_USER, "wrong")),
        await MqttTester().run(
            _credentials(dynsec_enabled_broker, DYNSEC_ADMIN_USER, DYNSEC_ADMIN_PASSWORD)
        ),
    ]
    rendered = repr(results)
    for secret in (PLATFORM_PASSWORD, DYNSEC_ADMIN_PASSWORD, "wrong"):
        assert secret not in rendered
    for record in caplog.records:
        for secret in (PLATFORM_PASSWORD, DYNSEC_ADMIN_PASSWORD):
            assert secret not in record.getMessage()


def test_the_client_never_reprs_its_password(dev_broker):
    """`BrokerCoordinates`' precedent (D66), which this client copies: a
    stray `%r` must not put a broker password in a log line."""
    client = _client(dev_broker, PLATFORM_USER, PLATFORM_PASSWORD)
    assert PLATFORM_PASSWORD not in repr(client)
    assert PLATFORM_PASSWORD not in str(client)
    assert PLATFORM_PASSWORD not in repr(client.coordinates())


# --- Registration -----------------------------------------------------------


def test_the_registry_carries_the_mqtt_tester_and_nothing_it_has_not_built():
    """E5.4b-e add their own keys. A tester registered before it exists would
    make the endpoint report a verdict nothing computed."""
    assert "mqtt" in REGISTRY
    assert isinstance(REGISTRY["mqtt"], MqttTester)
    assert set(REGISTRY) == {"mqtt"}


def test_resolve_credentials_carries_the_deployment_through():
    """The MQTT tester's topic is a function of the deployment, so the
    identity has to survive `resolve_credentials`. The other four ignore it,
    which is why it is keyword-only and defaulted."""
    from app.services.schemas import MqttSettings

    candidate = MqttSettings(
        host="broker.example",
        port=8883,
        username="platform-x",
        password="pw",
    )
    resolved = resolve_credentials(
        "mqtt",
        None,
        candidate,
        lambda name: "",
        deployment_id=DEPLOYMENT_ID,
        deployment_slug=SLUG,
    )
    assert resolved is not None
    assert resolved.deployment_id == DEPLOYMENT_ID
    assert resolved.deployment_slug == SLUG
    assert client_for(resolved).deployment_slug == SLUG


async def test_a_credential_set_without_a_deployment_is_a_contained_failure():
    """It cannot happen through the API - `MqttSettings` requires the four
    coordinates and the `mqtt_coordinates_required` CHECK requires them of a
    stored row - so it is a platform defect, and it must degrade to this
    service's `fail` rather than to a 500 that hides the other four."""
    credentials = ServiceCredentials(
        service_key="mqtt",
        settings={"host": "h", "port": 8883, "username": "u", "tls_enabled": True},
        secrets={"password": "p"},
    )
    with pytest.raises(ValueError):
        client_for(credentials)

    result = await MqttTester().run(credentials)
    assert result.outcome == "fail"
    assert [check.name for check in result.checks] == ["settings"]
    assert result.checks[0].remedy
