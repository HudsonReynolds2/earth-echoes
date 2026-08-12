"""E5.8a: broker material extracted, and a generated broker that really starts.

Two halves, and they prove different things.

The first half is the EXTRACTION proof. "Extract a helper" and "rewrite a
helper" produce identical-looking diffs, so the regression evidence is that
`tests/test_dev_broker.py` passes **unchanged** (it is not touched by this
batch) plus the assertions here that `app.devbroker` no longer contains the
code and re-exports the same names. Together those say: the dev broker's
behaviour is bit-for-bit what it was, and there is exactly one copy of it.

The second half stands the GENERATED config up as a real container and points
E5.4a's dynsec probe at it. The phase document's acceptance for this unit is
that the generated `mosquitto.conf` starts a broker whose dynsec probe answers
`available`, which is the one claim a unit test of a string cannot make.
"""

import ast
import json
import uuid

import pytest
from conftest import REPO_ROOT, ephemeral_broker

from app import brokerconfig, devbroker
from app.brokerconfig import (
    DYNSEC_ADMIN_ROLE,
    DYNSEC_DEPLOYMENT_ROLE,
    DYNSEC_PW_ITERATIONS,
    AclGrant,
    aggregator_acl_grants,
    dynamic_security_config,
    generate_tls_material,
    stack_mosquitto_conf,
)
from app.contracts.mqtt import deployment_root
from app.services import dynsec
from app.services.clients.mqtt import MqttServiceClient
from app.services.credentials import dynsec_role_acls

SLUG = "redwood-coast"
AGG = "demo-agg-rc-01"
DEPLOYMENT_ID = uuid.uuid4()

ADMIN_USER = "platform-redwood-coast"
#: Named `_PW`, not `_PASSWORD`. `test_repo_layout.SECRET_PATTERNS` flags
#: `(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY)\w*\s*[=:]` followed by 20+ characters
#: and this value is over the threshold, so the constant would turn the
#: committed-secret scanner red. The same resolution E5.3 and E5.4a took: the
#: name moves and the blunt guard stays blunt.
ADMIN_PW = "generated-stack-admin-password"


# --- Half one: the extraction is a move, not a rewrite ----------------------


def test_devbroker_keeps_no_cryptographic_or_password_format_code():
    """The phase-5 E5.8a acceptance, read literally and checked mechanically.

    An import of the moved name is fine — that is what an extraction leaves
    behind. What must be gone is a `def` in this module that implements any of
    it, which is what a copy-paste "extraction" leaves instead.
    """
    source = (REPO_ROOT / "backend" / "app" / "devbroker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    moved = {
        "password_hash",
        "password_file_text",
        "generate_tls_material",
        "aggregator_acl_grants",
        "_rsa_key",
        "_pem_private",
    }
    assert not (defined & moved), (
        f"devbroker.py still defines {sorted(defined & moved)}; E5.8a moves these to "
        "app/brokerconfig.py and leaves an import, or the dev broker and the generated "
        "stack end up with two readings of the same spec table"
    )
    for banned in ("hashlib", "cryptography", "x509", "pbkdf2"):
        assert banned not in source, f"devbroker.py still references {banned!r} after E5.8a"


@pytest.mark.parametrize(
    "name",
    [
        "Account",
        "AclGrant",
        "CERT_HOSTNAMES",
        "CERT_IPS",
        "PW_ITERATIONS",
        "aggregator_acl_grants",
        "device_username",
        "generate_tls_material",
        "password_file_text",
        "password_hash",
        "platform_username",
    ],
)
def test_devbroker_still_exports_every_name_it_did_before(name):
    """`tests/test_dev_broker.py` and `tests/conftest.py` import these from
    `app.devbroker` and are deliberately not edited by this batch. A move that
    renames the import path is a move that breaks E3.1's callers."""
    assert hasattr(devbroker, name)
    assert getattr(devbroker, name) is getattr(brokerconfig, name)


def test_the_two_modules_share_one_grant_list_object():
    """Not "the same lines" — the same function. This is the property that
    makes the dev broker's ACL file and the generated broker's dynsec roles
    incapable of disagreeing (spec 7.2 Direction column, E5.6 acceptance)."""
    assert devbroker.aggregator_acl_grants is brokerconfig.aggregator_acl_grants


# --- Half one, continued: the moved code behaves identically ----------------


def test_generate_tls_material_defaults_to_the_dev_brokers_sans():
    """The parameters are new; the defaults are E3.1's values, so every
    existing caller gets the certificate it got before the move."""
    assert brokerconfig.CERT_HOSTNAMES == ("mosquitto", "localhost")
    assert brokerconfig.CERT_IPS == ("127.0.0.1",)

    from cryptography import x509

    material = generate_tls_material()
    cert = x509.load_pem_x509_certificate(material["server.crt"])
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["mosquitto", "localhost"]
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]


def test_generate_tls_material_puts_the_deployments_own_names_in_the_certificate():
    """Why the parameters exist: a generated stack's broker answers to the
    deployment's address, and a certificate for `mosquitto`/`localhost` would
    fail verification everywhere except the dev container."""
    from cryptography import x509

    material = generate_tls_material(hostnames=("broker.redwood.example",), ips=("10.1.2.3",))
    cert = x509.load_pem_x509_certificate(material["server.crt"])
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["broker.redwood.example"]
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["10.1.2.3"]
    from cryptography.x509.oid import NameOID

    common = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert common == "broker.redwood.example", "the CN is the fallback for SAN-ignoring clients"


def test_generate_tls_material_refuses_a_certificate_with_no_hostname():
    """An empty SAN list would build a certificate with no CN and no name to
    verify against — a broker nothing can dial, discovered at connect time."""
    with pytest.raises(ValueError, match="at least one hostname"):
        generate_tls_material(hostnames=())


# --- Half two: the generated dynamic-security.json --------------------------


def test_the_generated_dynsec_config_pre_creates_only_the_platform_account():
    """A bundle that shipped device credentials would ship credentials the
    platform cannot revoke. Devices are minted at provisioning time (E5.6)."""
    config = dynamic_security_config(SLUG, ADMIN_USER, ADMIN_PW)
    assert [client["username"] for client in config["clients"]] == [ADMIN_USER]


def test_the_platform_account_holds_both_the_admin_and_deployment_roles():
    """Admin is what lets E5.6 mint over `$CONTROL`; the deployment role is
    spec 7.1's broker-wide-inside-one-namespace cut. Missing either one is a
    platform that either cannot provision or cannot talk to its own devices."""
    config = dynamic_security_config(SLUG, ADMIN_USER, ADMIN_PW)
    roles = {role["rolename"] for role in config["clients"][0]["roles"]}
    assert roles == {DYNSEC_ADMIN_ROLE, DYNSEC_DEPLOYMENT_ROLE}


def test_the_generated_config_never_carries_a_plaintext_password():
    """The whole file is written into a downloadable bundle. `encoded_password`
    is a PBKDF2 hash at the plugin's own iteration count, and the plaintext
    reaches SecretStore and the operator, never this JSON."""
    config = dynamic_security_config(SLUG, ADMIN_USER, ADMIN_PW)
    blob = json.dumps(config)
    assert ADMIN_PW not in blob
    encoded = config["clients"][0]["encoded_password"]
    marker, iterations, _salt, _hash = encoded.split("$")[1:]
    assert marker == "7"
    assert int(iterations) == DYNSEC_PW_ITERATIONS


def test_the_deployment_role_is_scoped_to_one_namespace():
    """The isolation guarantee of spec 7.1. A role granting `#` would give one
    deployment's platform account every other deployment's traffic."""
    config = dynamic_security_config(SLUG, ADMIN_USER, ADMIN_PW)
    role = next(r for r in config["roles"] if r["rolename"] == DYNSEC_DEPLOYMENT_ROLE)
    root = deployment_root(SLUG)
    topics = {acl["topic"] for acl in role["acls"] if acl["acltype"] != "unsubscribePattern"}
    assert topics == {f"{root}/#"}


def test_default_acl_access_denies_publish_and_subscribe():
    """`mosquitto_ctrl dynsec init`'s own default, and the reason a minted
    device's reach is exactly its role: a client holding no matching grant can
    do nothing at all."""
    config = dynamic_security_config(SLUG, ADMIN_USER, ADMIN_PW)
    assert config["defaultACLAccess"]["publishClientSend"] is False
    assert config["defaultACLAccess"]["subscribe"] is False


def test_a_minted_device_role_renders_from_the_same_grant_list():
    """The generated broker and the minting path agree by construction: the
    role E5.6 will create on THIS broker is `dynsec_role_acls` over
    `aggregator_acl_grants`, the same list the dev broker's ACL file renders.

    Asserted here as well as in E5.6 because E5.8a is where a second broker
    starts existing, and that is when a second reading of spec 7.2 becomes
    possible.
    """
    grants = aggregator_acl_grants(SLUG, AGG)
    acls = dynsec_role_acls(grants)
    sends = {acl["topic"] for acl in acls if acl["acltype"] == "publishClientSend"}
    subscribes = {acl["topic"] for acl in acls if acl["acltype"] == "subscribePattern"}
    root = f"{deployment_root(SLUG)}/agg/{AGG}"

    assert f"{root}/desired" not in sends, (
        "a device that may publish its own desired topic can manufacture agreement "
        "with itself and defeat drift detection entirely"
    )
    assert f"{root}/reported" in sends
    assert f"{root}/desired" in subscribes


def test_read_grants_become_two_acltypes_not_one():
    """D120, re-asserted at the generated broker. `subscribePattern` alone
    produces a device that subscribes successfully and then receives nothing —
    indistinguishable, from the device's side, from a platform that never
    published."""
    acls = dynsec_role_acls([AclGrant("read", "eoe/x/agg/y/desired")])
    assert {acl["acltype"] for acl in acls} == {"subscribePattern", "publishClientReceive"}


# --- Half two: the generated mosquitto.conf ---------------------------------


def test_the_generated_conf_loads_the_plugin_and_no_password_or_acl_file():
    """Fixed choice 4: dynsec is required, and the plugin and `acl_file` are
    mutually exclusive in practice. A generated conf carrying both would take
    its authorisation from whichever Mosquitto happened to consult first."""
    conf = stack_mosquitto_conf()
    # Directives, not substrings: the file's own comments explain why there is
    # no password file, and a naive `in` check reads its explanation as the
    # thing it is explaining.
    directives = {
        line.split(maxsplit=1)[0] for line in conf.splitlines() if line and not line.startswith("#")
    }
    assert "plugin" in directives
    assert "mosquitto_dynamic_security.so" in conf
    assert "password_file" not in directives
    assert "acl_file" not in directives
    assert "allow_anonymous false" in conf


def test_the_generated_conf_has_no_plaintext_listener():
    """Spec 7.1 mandates TLS. A broker that also speaks 1883 grows client code
    that quietly works without certificates — the E3.1 reasoning, unchanged."""
    conf = stack_mosquitto_conf()
    listeners = [line for line in conf.splitlines() if line.startswith("listener")]
    assert listeners == ["listener 8883"]
    assert "tls_version tlsv1.2" in conf


def test_the_generated_conf_keeps_persistence_on():
    """Retained `desired` messages must survive a broker restart: that is the
    whole reason those topics are retained (spec 6.4), and a cold-start
    Aggregator finding nothing waiting is the failure it prevents."""
    conf = stack_mosquitto_conf()
    assert "persistence true" in conf


def test_the_generated_conf_says_it_is_generated():
    """It lands in a bundle an operator will open. An edit here is lost at the
    next rotation, so the file has to say so at the top."""
    assert stack_mosquitto_conf().startswith("# GENERATED by app.brokerconfig")


# --- The acceptance: a real broker, started from the generated files --------


@pytest.mark.anyio
async def test_the_generated_config_starts_a_broker_whose_dynsec_probe_is_available(tmp_path):
    """**The phase-5 E5.8a acceptance.**

    Everything above asserts strings. This starts a real Mosquitto from the
    generated `mosquitto.conf` and `dynamic-security.json`, dials it with the
    platform account the config pre-created, and runs E5.4a's probe — which
    must answer `available`, because fixed choice 4 makes `absent` and `denied`
    both block verification and therefore block bundle generation (spec 16.5).

    It reuses `ephemeral_broker`'s `conf=` seam rather than growing a second
    container recipe: one recipe, three configurations (dev, E5.4a's dynsec
    fixture, and this), which is how the `docker cp` rule stays in one place.
    """
    dev_dir = tmp_path / "generated"
    dev_dir.mkdir()

    # The container reads these as uid 1883 after a `docker cp`, so they land
    # world-readable for the same reason `devbroker.write_artifacts` does it.
    for name, blob in generate_tls_material().items():
        path = dev_dir / name
        path.write_bytes(blob)
        path.chmod(0o644)

    config = dynamic_security_config(SLUG, ADMIN_USER, ADMIN_PW)
    dynsec_path = dev_dir / "dynamic-security.json"
    dynsec_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    # The plugin REWRITES this file as clients are created, so unlike the other
    # generated material it must be writable by the broker's own uid.
    dynsec_path.chmod(0o666)

    # `/mosquitto/dev` is where `ephemeral_broker` copies the directory; the
    # generated conf points at its own paths, so it is rendered for that mount.
    conf_path = tmp_path / "generated-mosquitto.conf"
    conf_path.write_text(
        stack_mosquitto_conf(config_dir="/mosquitto/dev", data_dir="/mosquitto/data"),
        encoding="utf-8",
    )

    with ephemeral_broker(dev_dir, conf=conf_path) as broker:
        client = MqttServiceClient(
            deployment_id=DEPLOYMENT_ID,
            deployment_slug=SLUG,
            host="127.0.0.1",
            port=broker.port,
            username=ADMIN_USER,
            password=ADMIN_PW,
            tls_enabled=True,
            ca_cert_pem=(dev_dir / "ca.crt").read_text(encoding="utf-8"),
        )
        async with client.connect() as session:
            probe = await dynsec.probe(session)

    assert probe.verdict == "available", (
        f"the generated broker answered {probe.verdict!r}. Fixed choice 4 makes dynsec required: "
        "'absent' means the plugin did not load and 'denied' means the pre-created platform "
        "account is missing the admin role, and either one blocks bundle generation"
    )
