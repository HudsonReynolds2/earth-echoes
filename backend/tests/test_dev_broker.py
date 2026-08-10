"""Gate 39: E3.1 development broker (spec 7.1).

The acceptance criteria in the phase document are the two integration tests at
the bottom: one dev device cannot read another's subtree, and the platform
account can reach its whole deployment namespace. Everything above them proves
the generated material is right BEFORE a broker is involved, so a red ACL test
means the ACL is wrong rather than the password format.
"""

import base64
import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from conftest import REPO_ROOT, ephemeral_broker, ephemeral_postgres, make_kek
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import select

from app.contracts.mqtt import aggregator_root, deployment_root
from app.db import create_session_factory
from app.devbroker import (
    Account,
    acl_file_text,
    device_username,
    generate_tls_material,
    load_manifest,
    password_file_text,
    password_hash,
    plan_accounts,
    platform_username,
    secret_name,
)
from app.models import Deployment, DeploymentService
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy

BACKEND = REPO_ROOT / "backend"

RC = "redwood-coast"
HD = "high-desert"
AGG_A = "demo-agg-rc-01"
AGG_B = "demo-agg-rc-02"


# --- The Mosquitto password file -------------------------------------------


def test_password_hash_uses_the_mosquitto_pbkdf2_field_layout():
    salt = b"0123456789ab"
    line = password_hash("hunter2", salt, iterations=101)
    marker, iterations, encoded_salt, encoded_hash = line.split("$")[1:]
    assert marker == "7", "Mosquitto 2.x reads $7$ as PBKDF2-SHA512"
    assert iterations == "101"
    assert base64.b64decode(encoded_salt) == salt
    assert base64.b64decode(encoded_hash) == hashlib.pbkdf2_hmac(
        "sha512", b"hunter2", salt, 101, 64
    )


def test_password_file_salts_every_line_independently():
    accounts = [
        Account("a", "same-password", "platform", RC),
        Account("b", "same-password", "platform", HD),
    ]
    first, second = password_file_text(accounts).strip().splitlines()
    assert first.split(":", 1)[1] != second.split(":", 1)[1], "identical passwords share a salt"


def test_password_file_never_contains_a_plaintext_password():
    text = password_file_text([Account("a", "s3cr3t-value", "platform", RC)])
    assert "s3cr3t-value" not in text


# --- The ACL file: spec 7.2's Direction column, made enforceable ------------


def _acl_for(username: str, text: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(f"user {username}")
    grants = []
    for line in lines[start + 1 :]:
        if line.startswith("user "):
            break
        if line.startswith("topic "):
            grants.append(line)
    return grants


def test_platform_account_gets_its_whole_deployment_namespace():
    text = acl_file_text([Account(platform_username(RC), "p", "platform", RC)])
    assert _acl_for(platform_username(RC), text) == [f"topic readwrite {deployment_root(RC)}/#"]


def test_device_grants_match_the_spec_7_2_direction_table():
    account = Account(device_username(AGG_A), "p", "device", RC, AGG_A)
    grants = _acl_for(device_username(AGG_A), acl_file_text([account]))
    root = aggregator_root(RC, AGG_A)
    assert grants == [
        f"topic read {root}/desired",
        f"topic read {root}/cmd",
        f"topic read {root}/lst/+/desired",
        f"topic write {root}/reported",
        f"topic write {root}/status",
        f"topic write {root}/event",
        f"topic write {root}/lst/+/reported",
    ]


def test_a_device_is_granted_nothing_outside_its_own_subtree():
    text = acl_file_text(
        [
            Account(device_username(AGG_A), "p", "device", RC, AGG_A),
            Account(device_username(AGG_B), "p", "device", RC, AGG_B),
        ]
    )
    for grant in _acl_for(device_username(AGG_A), text):
        assert aggregator_root(RC, AGG_B) not in grant
        # The platform writes desired; a device that could write its own
        # desired topic could forge its way out of drift (spec 6.1).
        assert not grant.startswith(f"topic write {aggregator_root(RC, AGG_A)}/desired")


# --- TLS material -----------------------------------------------------------


def test_server_certificate_is_signed_by_the_generated_ca_and_names_both_hosts():
    material = generate_tls_material()
    ca = x509.load_pem_x509_certificate(material["ca.crt"])
    server = x509.load_pem_x509_certificate(material["server.crt"])
    assert server.issuer == ca.subject
    ca.public_key().verify(  # type: ignore[call-arg,union-attr]
        server.signature,
        server.tbs_certificate_bytes,
        padding.PKCS1v15(),
        server.signature_hash_algorithm,
    )
    names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "mosquitto" in names.get_values_for_type(x509.DNSName)
    assert "localhost" in names.get_values_for_type(x509.DNSName)


# --- Database side ----------------------------------------------------------


@pytest.mark.integration
def test_plan_accounts_covers_every_deployment_and_aggregator():
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
            accounts = plan_accounts(db)

    platform = {a.deployment_slug for a in accounts if a.kind == "platform"}
    devices = {a.aggregator_uuid for a in accounts if a.kind == "device"}
    assert platform == {RC, HD}
    assert devices == {
        "demo-agg-rc-01",
        "demo-agg-rc-02",
        "demo-agg-rc-03",
        "demo-agg-hd-01",
        "demo-agg-hd-02",
        "demo-agg-hd-03",
    }
    assert len({a.password for a in accounts}) == len(accounts), "passwords are not distinct"


@pytest.mark.integration
def test_service_rows_carry_coordinates_and_the_password_only_through_secretstore(tmp_path):
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        _provision(url, kek, tmp_path)

        store = SecretStore(factory, kek)
        with factory() as db:
            rows = list(db.scalars(select(DeploymentService)))
            assert len(rows) == 2
            slugs = {db.get(Deployment, row.deployment_id).slug for row in rows}
            assert slugs == {RC, HD}
            for row in rows:
                assert row.service_key == "mqtt"
                assert row.tls_enabled is True
                assert row.ca_cert_pem.startswith("-----BEGIN CERTIFICATE-----")
                assert row.password_secret_name == secret_name(row.deployment_id)
                # The row names a secret; it never holds one (rule R2).
                assert store.get(row.password_secret_name)

    manifest = load_manifest(tmp_path)
    by_username = {a["username"]: a for a in manifest["accounts"]}
    assert by_username[platform_username(RC)]["kind"] == "platform"
    assert by_username[device_username(AGG_A)]["deployment_slug"] == RC


def _provision(url: str, kek: str, out: Path, host: str = "localhost") -> None:
    """Run the generator exactly as an operator would, in a real subprocess."""
    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--out", str(out), "--host", host],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": url,
            "EOE_SESSION_SECRET": f"devbroker-{uuid.uuid4().hex}",
            "EOE_KEK": kek,
        },
        timeout=180,
    )
    assert result.returncode == 0, f"devbroker failed:\n{result.stdout}\n{result.stderr}"


# --- The acceptance tests: a real broker enforcing the real ACL -------------


def _password(manifest: dict, username: str) -> str:
    for account in manifest["accounts"]:
        if account["username"] == username:
            return account["password"]
    raise AssertionError(f"no account {username!r} in the manifest")


def _client_args(username: str, password: str, topic: str) -> list[str]:
    return [
        "-h",
        "localhost",
        "-p",
        "8883",
        "--cafile",
        "/mosquitto/dev/ca.crt",
        "-u",
        username,
        "-P",
        password,
        "-t",
        topic,
    ]


#: mosquitto_sub exit codes we assert on. Mosquitto accepts a subscription to
#: a topic the ACL denies (SUBACK 0) and then simply never delivers, so DENIAL
#: IS A TIMEOUT, not a protocol error — which is exactly why every denial
#: assertion below is paired with a positive control publishing the same
#: retained message to the same topic for an authorized identity.
TIMED_OUT = 27
NOT_AUTHORISED = 5


def _subscribe(broker, username: str, password: str, topic: str, *, wait: int = 3):
    """-W bounds the wait so a denied subscription cannot hang the gate;
    -C 1 returns as soon as the (retained) message arrives."""
    return broker.exec_client(
        "mosquitto_sub",
        *_client_args(username, password, topic),
        "-W",
        str(wait),
        "-C",
        "1",
        timeout=60,
    )


def _sweep(broker, username: str, password: str, topic: str, *, wait: int = 3) -> dict[str, str]:
    """Everything a subscription actually delivers inside `wait` seconds, as
    {topic: payload}. No -C, so the client collects rather than stopping at the
    first message: the interesting question for a wildcard is what the whole
    set contains, not whether anything arrived at all."""
    result = broker.exec_client(
        "mosquitto_sub",
        *_client_args(username, password, topic),
        "-v",
        "-W",
        str(wait),
        timeout=60,
    )
    delivered = {}
    for line in result.stdout.splitlines():
        if line.strip():
            received_topic, _, payload = line.partition(" ")
            delivered[received_topic] = payload
    return delivered


def _publish(broker, username: str, password: str, topic: str, payload: str):
    return broker.exec_client(
        "mosquitto_pub",
        *_client_args(username, password, topic),
        "-m",
        payload,
        "-q",
        "1",
        "-r",
        timeout=60,
    )


#: Retained markers the fixture leaves on three desired topics. Retained is
#: what makes these assertions deterministic: a permitted subscriber gets the
#: message the instant it subscribes, with no publisher racing the test.
MARK_A = "marker-rc-agg-01"
MARK_B = "marker-rc-agg-02"
MARK_HD = "marker-hd-agg-01"


@pytest.fixture(scope="module")
def provisioned_broker(tmp_path_factory):
    """One provisioned broker for the whole acceptance set: the container and
    its Postgres are expensive, and every assertion below reads the same ACL
    file. Seeds one retained marker per subtree the tests reach for."""
    out = tmp_path_factory.mktemp("dev-certs")
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        _provision(url, kek, out)
        with ephemeral_broker(out) as broker:
            manifest = load_manifest(out)
            markers = (
                (RC, f"{aggregator_root(RC, AGG_A)}/desired", MARK_A),
                (RC, f"{aggregator_root(RC, AGG_B)}/desired", MARK_B),
                (HD, f"{aggregator_root(HD, 'demo-agg-hd-01')}/desired", MARK_HD),
            )
            for slug, topic, payload in markers:
                user = platform_username(slug)
                published = _publish(broker, user, _password(manifest, user), topic, payload)
                assert published.returncode == 0, published.stderr
            yield broker, manifest


@pytest.mark.integration
def test_platform_account_reaches_the_whole_deployment_namespace(provisioned_broker):
    """The second E3.1 acceptance criterion. One wildcard subscription over
    the deployment root must reach a topic the platform never published to
    itself — the aggregator's, not the platform's, half of the namespace."""
    broker, manifest = provisioned_broker
    user = platform_username(RC)
    password = _password(manifest, user)

    # A device publishes into its own subtree; the platform must see it on a
    # single sweep of the deployment root, together with both aggregators'
    # desired topics — the whole namespace, not just what it published itself.
    device = device_username(AGG_A)
    reported = f"{aggregator_root(RC, AGG_A)}/reported"
    device_published = _publish(broker, device, _password(manifest, device), reported, "from-agg")
    assert device_published.returncode == 0, device_published.stderr

    swept = _sweep(broker, user, password, f"{deployment_root(RC)}/#")
    assert swept.get(reported) == "from-agg", f"platform missed a device publish: {swept}"
    assert swept.get(f"{aggregator_root(RC, AGG_A)}/desired") == MARK_A
    assert swept.get(f"{aggregator_root(RC, AGG_B)}/desired") == MARK_B


@pytest.mark.integration
def test_a_device_cannot_read_another_devices_subtree(provisioned_broker):
    """THE E3.1 acceptance criterion, stated in the phase document.

    Both halves subscribe to a topic carrying a retained marker, so the only
    difference between them is the credential."""
    broker, manifest = provisioned_broker
    device = device_username(AGG_A)
    password = _password(manifest, device)

    mine = _subscribe(broker, device, password, f"{aggregator_root(RC, AGG_A)}/desired")
    assert mine.returncode == 0 and mine.stdout.strip() == MARK_A, (
        f"control failed - the device cannot read its OWN subtree: {mine.stderr}"
    )

    theirs = _subscribe(broker, device, password, f"{aggregator_root(RC, AGG_B)}/desired")
    assert theirs.returncode == TIMED_OUT, "the broker allowed a cross-device subscription"
    assert MARK_B not in theirs.stdout


@pytest.mark.integration
def test_a_device_cannot_reach_another_subtree_with_a_wildcard(provisioned_broker):
    """The obvious escape: subscribe to the deployment root and take
    everything. Mosquitto accepts the wildcard and then filters PER MESSAGE
    against the ACL, so the sweep must come back holding exactly the device's
    own grants and nothing belonging to its neighbours."""
    broker, manifest = provisioned_broker
    device = device_username(AGG_A)
    swept = _sweep(broker, device, _password(manifest, device), f"{deployment_root(RC)}/#")

    assert swept == {f"{aggregator_root(RC, AGG_A)}/desired": MARK_A}, (
        f"a device swept beyond its own subtree: {swept}"
    )


@pytest.mark.integration
def test_topic_direction_is_enforced_not_just_the_subtree(provisioned_broker):
    """Spec 7.2's Direction column is a security boundary, not documentation:
    a device that could subscribe to its own status topic, or publish its own
    desired config, could manufacture agreement and defeat drift detection."""
    broker, manifest = provisioned_broker
    device = device_username(AGG_A)
    password = _password(manifest, device)
    root = aggregator_root(RC, AGG_A)

    forged = _publish(broker, device, password, f"{root}/desired", "forged")
    assert forged.returncode == 0, "publish is fire-and-forget; the ACL check is server-side"
    still_original = _subscribe(broker, device, password, f"{root}/desired")
    assert still_original.stdout.strip() == MARK_A, "a device overwrote its own desired config"

    platform = platform_username(RC)
    _publish(broker, platform, _password(manifest, platform), f"{root}/status", "online")
    write_only = _subscribe(broker, device, password, f"{root}/status")
    assert write_only.returncode == TIMED_OUT, "a device read a topic it may only write"


@pytest.mark.integration
def test_deployments_cannot_see_each_other(provisioned_broker):
    broker, manifest = provisioned_broker
    user = platform_username(RC)
    password = _password(manifest, user)

    own = _subscribe(broker, user, password, f"{aggregator_root(RC, AGG_A)}/desired")
    assert own.stdout.strip() == MARK_A, "control failed - platform cannot read its own namespace"

    other = _subscribe(broker, user, password, f"{aggregator_root(HD, 'demo-agg-hd-01')}/desired")
    assert other.returncode == TIMED_OUT, "a platform account read another deployment's namespace"
    assert MARK_HD not in other.stdout


@pytest.mark.integration
def test_the_broker_refuses_a_wrong_password_and_an_unknown_account(provisioned_broker):
    broker, manifest = provisioned_broker
    user = platform_username(RC)

    bad = _subscribe(broker, user, "not-the-password", f"{deployment_root(RC)}/#")
    assert bad.returncode == NOT_AUTHORISED, bad.stderr
    unknown = _subscribe(broker, "nobody", "nothing", f"{deployment_root(RC)}/#")
    assert unknown.returncode == NOT_AUTHORISED, unknown.stderr


@pytest.mark.integration
def test_the_listener_is_tls_only(provisioned_broker):
    """Spec 7.1 puts the control plane on TLS. A plaintext client must fail at
    the protocol level, which is the proof there is no 1883 fallback growing
    quietly beside the TLS listener."""
    broker, manifest = provisioned_broker
    user = platform_username(RC)
    plaintext = broker.exec_client(
        "mosquitto_sub",
        "-h",
        "localhost",
        "-p",
        "8883",
        "-u",
        user,
        "-P",
        _password(manifest, user),
        "-t",
        f"{deployment_root(RC)}/#",
        "-W",
        "3",
        "-C",
        "1",
        timeout=60,
    )
    assert plaintext.returncode not in (0, TIMED_OUT), "a plaintext client reached the broker"
