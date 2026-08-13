"""Broker material: the things that make a Mosquitto, wherever it is going.

Extracted from `app.devbroker` at task E5.8a. Nothing here knows about the
development stack, the database or the CLI — it turns arguments into bytes a
broker will accept, and both callers use it:

* `app.devbroker` (E3.1) writes the development broker's `passwd`/`acl` pair
  and its TLS material into `deploy/dev-certs/`.
* the E5.8b stack generator renders a deployment's own broker into a bundle,
  with the dynamic security plugin in place of the ACL file (fixed choice 4).

**Why the move happened.** Two brokers now need the same PBKDF2 field layout,
the same certificate shape and the same answer to "what may an Aggregator do",
and the second one is generated for real deployments rather than for a dev
container. A copy in the stack generator would be a second reading of spec 7.2
that drifts from the first, and the drift that matters is a device permitted to
publish its own `desired` topic — which lets it manufacture agreement with
itself and defeats drift detection. So there is one renderer, and
`aggregator_acl_grants` below is the one list it renders from.
"""

import base64
import datetime as dt
import hashlib
import ipaddress
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.contracts.mqtt import aggregator_root, deployment_root

#: Mosquitto 2.x PBKDF2-SHA512 password file format; 101 iterations and a
#: 12-byte salt are `mosquitto_passwd`'s own defaults.
PW_ITERATIONS = 101
PW_SALT_BYTES = 12
PW_HASH_BYTES = 64

#: The dynamic security plugin's default iteration count, which is what
#: `mosquitto_ctrl dynsec init` writes. The password FORMAT is identical to the
#: password file's — same `$7$` PBKDF2-HMAC-SHA512 layout — and only the count
#: differs, so `password_hash` serves both and a mismatch here would surface as
#: an authentication failure and nothing subtler.
DYNSEC_PW_ITERATIONS = 1000

#: SAN entries the DEVELOPMENT broker answers to: `mosquitto` inside the
#: compose network, localhost/127.0.0.1 from the host and from the test suite.
#: Defaults only — a generated stack passes its own deployment's hostnames.
CERT_HOSTNAMES = ("mosquitto", "localhost")
CERT_IPS = ("127.0.0.1",)


@dataclass(frozen=True)
class Account:
    """One broker login. `kind` decides the ACL cut: a platform account gets
    its whole deployment namespace, a device account gets exactly the spec 7.2
    direction table for its own subtree — read what the platform sends it,
    write what it reports back, and nothing else, in either direction."""

    username: str
    password: str
    kind: str  # "platform" | "device"
    deployment_slug: str
    aggregator_uuid: str | None = None


def platform_username(slug: str) -> str:
    return f"platform-{slug}"


def device_username(aggregator_uuid: str) -> str:
    return f"dev-{aggregator_uuid}"


# --- Mosquitto password format ----------------------------------------------


def password_hash(password: str, salt: bytes, iterations: int = PW_ITERATIONS) -> str:
    """One `$7$` field group exactly as Mosquitto 2.x writes it: PBKDF2-HMAC-
    SHA512 over the raw password with the raw salt, both halves base64'd.

    Used for both the password file (101 iterations) and the dynamic security
    plugin's `encoded_password` (1000); the layout is the same and only the
    count differs, which is why `iterations` is a parameter.
    """
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations, PW_HASH_BYTES)
    encoded_salt = base64.b64encode(salt).decode()
    encoded_hash = base64.b64encode(digest).decode()
    return f"$7${iterations}${encoded_salt}${encoded_hash}"


def password_file_text(accounts: Sequence[Account]) -> str:
    """The whole password file. Mosquitto 2.0 dropped plain-text password
    files, so every line carries a hash and the plaintext exists only in
    `accounts.json` and SecretStore."""
    lines = [
        f"{account.username}:{password_hash(account.password, os.urandom(PW_SALT_BYTES))}"
        for account in accounts
    ]
    return "".join(f"{line}\n" for line in lines)


# --- The one ACL grant list -------------------------------------------------


@dataclass(frozen=True)
class AclGrant:
    """One permission an Aggregator holds on one topic filter.

    `access` is the `acl_file` vocabulary (`read` = the device may subscribe
    and be delivered to, `write` = the device may publish) because that file
    is where these grants were first written down. `dynsec_role_acls` in
    `app/services/credentials.py` translates it into the plugin's three
    acltypes; nothing else may re-derive it.
    """

    access: str  # read | write
    topic: str


def aggregator_acl_grants(slug: str, aggregator_uuid: str) -> tuple[AclGrant, ...]:
    """**The one list of what an Aggregator may do on its broker** (E5.6).

    Spec 7.2's Direction column read literally, and the isolation guarantee of
    spec 7.1: a device may read its own `desired`, `cmd` and its Listeners'
    `desired`, and may write its own `reported`, `status`, `event` and its
    Listeners' `reported`. Nothing else, in either direction.

    **Why this is a function and not two literal blocks.** Three authorization
    backends now render these grants — the dev broker's `acl_file` (E3.1), the
    dynamic security role each minted credential holds (E5.6), and the
    generated stack's broker (E5.8) — and two literal readings of the same spec
    table will eventually disagree. The disagreement that matters is a single
    missing line: an Aggregator that may publish to its own `desired` topic can
    manufacture agreement with itself and defeat drift detection entirely. So
    every renderer reads THIS, and a test asserts each against it (phase-5 E5.6
    acceptance).
    """
    root = aggregator_root(slug, aggregator_uuid)
    return (
        AclGrant("read", f"{root}/desired"),
        AclGrant("read", f"{root}/cmd"),
        AclGrant("read", f"{root}/lst/+/desired"),
        AclGrant("write", f"{root}/reported"),
        AclGrant("write", f"{root}/status"),
        AclGrant("write", f"{root}/event"),
        AclGrant("write", f"{root}/lst/+/reported"),
    )


# --- TLS material -----------------------------------------------------------


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_private(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def generate_tls_material(
    hostnames: Sequence[str] = CERT_HOSTNAMES,
    ips: Sequence[str] = CERT_IPS,
) -> dict[str, bytes]:
    """A private CA and one server certificate signed by it.

    The platform trusts the CA (stored on the `deployment_service` row) rather
    than disabling verification, so the TLS path is genuinely exercised instead
    of merely configured — that property holds for a generated stack exactly as
    it does for the dev broker.

    `hostnames` and `ips` are parameters because a generated stack's broker
    answers to the deployment's own address, not to `mosquitto`/`localhost`.
    The defaults are the dev broker's values, so E3.1's callers are unchanged.
    The certificate's CN is the first hostname, which is what a client that
    ignores SANs falls back to.
    """
    if not hostnames:
        raise ValueError("a server certificate needs at least one hostname for its CN")
    now = dt.datetime.now(dt.UTC)
    ca_key = _rsa_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Echoes of Earth dev CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    server_key = _rsa_key()
    alt_names: list[x509.GeneralName] = [x509.DNSName(host) for host in hostnames]
    alt_names += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return {
        "ca.crt": ca_cert.public_bytes(serialization.Encoding.PEM),
        "ca.key": _pem_private(ca_key),
        "server.crt": server_cert.public_bytes(serialization.Encoding.PEM),
        "server.key": _pem_private(server_key),
    }


# --- The generated deployment broker (task E5.8a) ---------------------------
#
# The dev broker authorises with `acl_file`; a generated stack authorises with
# the dynamic security plugin, because fixed choice 4 makes dynsec REQUIRED for
# v1 and E5.6 mints per-device credentials through it at runtime. The two are
# mutually exclusive in practice — with the plugin loaded, authentication and
# authorisation both come from its JSON — so the generated config carries
# neither `password_file` nor `acl_file`.

#: The dynsec role the platform account holds so E5.6 can mint device
#: credentials over `$CONTROL`. Named by the plugin, not by us.
DYNSEC_ADMIN_ROLE = "admin"

#: The role granting the platform its whole deployment namespace (spec 7.1).
DYNSEC_DEPLOYMENT_ROLE = "deployment"


def _acl(acltype: str, topic: str) -> dict[str, Any]:
    return {"acltype": acltype, "topic": topic, "allow": True}


def dynsec_password_hash(password: str) -> str:
    """Hash a password for `dynamic-security.json`, salting ONCE.

    Separate from `dynamic_security_config` because the salt is random and the
    rendered bundle must be byte-identical on every download (fixed choice 7):
    a config function that hashed its own argument would produce a different
    file each call, and the platform stores no blob to compare against. E5.9
    calls this once, stores the result, and every later render is pure.

    The stored form is the `$7$` field group, which is what E5.9 keeps in
    SecretStore. `dynsec_password_fields` splits it into the shape the plugin
    actually reads — see there for why those are not the same thing.
    """
    return password_hash(password, os.urandom(PW_SALT_BYTES), iterations=DYNSEC_PW_ITERATIONS)


def dynsec_password_fields(encoded: str) -> dict[str, Any]:
    """A `$7$` hash split into the plugin's THREE JSON fields.

    **Mosquitto 2.0's dynamic security plugin does not understand
    `encoded_password`.** It reads `password`, `salt` and `iterations` as
    separate members and silently ignores a client entry's `encoded_password`,
    which leaves the account with no password at all — every login is then
    refused with CONNACK 135 and the broker logs `not authorised`, with nothing
    said about the config it just skipped. Mosquitto 2.1 writes and reads the
    combined form instead.

    Measured on both, dialling a real broker with `mosquitto_sub`:

    | client entry                     | 2.0.20  | 2.1.2 |
    |----------------------------------|---------|-------|
    | `encoded_password`               | REFUSED | ok    |
    | `password` + `salt` + `iterations` | ok    | ok    |

    So the three-field form is what is rendered: it is the only one both
    versions accept, and `IMAGES["mosquitto"]` pins 2.0.x. This is not
    theoretical — E5.10's keystone found it by bringing the generated bundle up,
    and every dynsec test in the suite had passed because the FIXTURES run
    `eclipse-mosquitto:2`, a floating tag that now resolves to 2.1.2 (D132).

    Pure: same input, same output, so two downloads stay byte-identical.
    """
    marker, iterations, salt, digest = encoded.split("$")[1:]
    if marker != "7":
        raise ValueError(f"not a Mosquitto $7$ password hash: {marker!r}")
    return {"password": digest, "salt": salt, "iterations": int(iterations)}


def dynamic_security_config(
    deployment_slug: str,
    admin_username: str,
    admin_password_hash: str,
) -> dict[str, Any]:
    """The generated broker's `dynamic-security.json`, with the platform
    account pre-created and nothing else.

    Takes an ALREADY-HASHED password (see `dynsec_password_hash`) so that
    rendering is a pure function of stored state and two downloads of one
    bundle are byte-identical. It is split into the plugin's three password
    fields rather than written as one `encoded_password` string, because
    Mosquitto 2.0 reads only the former — `dynsec_password_fields` carries the
    measurement.

    **Only the platform account exists at generation time**, holding two roles:
    the plugin's `admin` role, which is what lets E5.6 create per-device clients
    over `$CONTROL/dynamic-security/v1`, and the deployment role, which is the
    broker-wide-inside-one-namespace cut spec 7.1 gives the platform. Device
    clients are minted at provisioning time by `DynsecCredentialProvider` and
    are deliberately absent here — a bundle that shipped device credentials
    would be a bundle whose credentials the platform cannot revoke.

    `defaultACLAccess` denies publish and subscribe by default, which is
    `mosquitto_ctrl dynsec init`'s own default: a client holding no matching
    grant can do nothing, so a device's reach is exactly its minted role.
    """
    root = deployment_root(deployment_slug)
    control = "$CONTROL/dynamic-security/#"
    return {
        "clients": [
            {
                "username": admin_username,
                **dynsec_password_fields(admin_password_hash),
                "roles": [
                    {"rolename": DYNSEC_ADMIN_ROLE},
                    {"rolename": DYNSEC_DEPLOYMENT_ROLE},
                ],
            }
        ],
        "roles": [
            {
                "rolename": DYNSEC_ADMIN_ROLE,
                "acls": [
                    _acl("publishClientSend", control),
                    _acl("publishClientReceive", control),
                    _acl("subscribePattern", control),
                ],
            },
            {
                "rolename": DYNSEC_DEPLOYMENT_ROLE,
                "acls": [
                    _acl("publishClientSend", f"{root}/#"),
                    _acl("publishClientReceive", f"{root}/#"),
                    _acl("subscribePattern", f"{root}/#"),
                    _acl("unsubscribePattern", "#"),
                ],
            },
        ],
        "defaultACLAccess": {
            "publishClientSend": False,
            "publishClientReceive": True,
            "subscribe": False,
            "unsubscribe": True,
        },
    }


def stack_mosquitto_conf(
    *,
    port: int = 8883,
    config_dir: str = "/mosquitto/config",
    data_dir: str = "/mosquitto/data",
) -> str:
    """The generated broker's `mosquitto.conf`.

    TLS only, for the same reason the dev broker is: spec 7.1 mandates TLS for
    the control plane, and a broker that also speaks 1883 grows client code
    that quietly works without certificates.

    Persistence is ON. Retained `desired` messages must survive a broker
    restart — that is the whole reason those topics are retained (spec 6.4), and
    a cold-start Aggregator finding nothing waiting is the failure it prevents.
    """
    return f"""\
# GENERATED by app.brokerconfig (task E5.8a) - do not edit by hand.
#
# Regenerate with the deployment's stack bundle; edits here are lost on the
# next rotation (POST /deployments/{{id}}/services/stack/rotate).

listener {port}
protocol mqtt

cafile {config_dir}/ca.crt
certfile {config_dir}/server.crt
keyfile {config_dir}/server.key

# Username/password over TLS. Client certificates are accepted if offered and
# never required: spec 7.1 scopes mTLS to "where the deployment supports it"
# and E8 owns it, so nothing here promises it.
require_certificate false
tls_version tlsv1.2

# Authentication and authorisation both come from the plugin (fixed choice 4),
# which is why there is no password_file and no acl_file. The platform account
# in dynamic-security.json holds the admin role and mints per-device
# credentials over $CONTROL at provisioning time (E5.6).
allow_anonymous false
plugin /usr/lib/mosquitto_dynamic_security.so
plugin_opt_config_file {config_dir}/dynamic-security.json

# Retained desired messages must survive a restart (spec 6.4).
persistence true
persistence_location {data_dir}/
autosave_interval 30

log_dest stdout
log_type error
log_type warning
log_type notice
connection_messages true
"""
