"""Dev broker provisioning (task E3.1; spec 7.1).

    uv run python -m app.devbroker --certs-only    # CA + server cert, no DB
    uv run python -m app.devbroker                 # + accounts, ACLs, service rows

Spec 7.1 puts a TLS Mosquitto beside every deployment, with the platform
holding broker-wide access inside that deployment's namespace and each
Aggregator restricted by per-topic ACL to its own subtree. Development gets
the same shape from day one — one container, real TLS, real per-device
credentials, real ACLs — because a control plane first exercised without TLS
grows code that quietly assumes plaintext.

Everything this writes is a THROWAWAY DEV ARTIFACT under `deploy/dev-certs/`,
which is gitignored: a private CA, a server certificate, generated passwords,
a Mosquitto password file and an ACL file. Nothing here belongs in a real
deployment, and re-running rotates every credential (the password file and
the SecretStore entries are rewritten in the same pass, so they cannot drift
apart). E4/E5 own real per-device credential minting; this is dev scaffolding.

Two passes exist because of a bootstrap order: Mosquitto will not start
without its password and ACL files, but the accounts in them come from the
database that starts beside it. `--certs-only` writes the certificates and
empty account files so the stack can come up; the full run then fills in the
accounts and reloads the broker.
"""

import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.mqtt import aggregator_root, deployment_root
from app.db import create_session_factory
from app.models import Aggregator, Deployment, DeploymentService, Pod
from app.secrets import SecretStore
from app.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_CERTS = REPO_ROOT / "deploy" / "dev-certs"

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8883

#: Mosquitto 2.x PBKDF2-SHA512 password file format; 101 iterations and a
#: 12-byte salt are `mosquitto_passwd`'s own defaults.
PW_ITERATIONS = 101
PW_SALT_BYTES = 12
PW_HASH_BYTES = 64

#: SAN entries the dev broker answers to: `mosquitto` inside the compose
#: network, localhost/127.0.0.1 from the host and from the test suite.
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


def secret_name(deployment_id: object, service_key: str = "mqtt") -> str:
    """The SecretStore name for a service password (INTERFACES, SecretStore
    section: the `deployment:{id}:*` namespace)."""
    return f"deployment:{deployment_id}:{service_key}_password"


# --- Mosquitto password file ------------------------------------------------


def password_hash(password: str, salt: bytes, iterations: int = PW_ITERATIONS) -> str:
    """One `$7$` field group exactly as Mosquitto 2.x writes it: PBKDF2-HMAC-
    SHA512 over the raw password with the raw salt, both halves base64'd."""
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations, PW_HASH_BYTES)
    encoded_salt = base64.b64encode(salt).decode()
    encoded_hash = base64.b64encode(digest).decode()
    return f"$7${iterations}${encoded_salt}${encoded_hash}"


def password_file_text(accounts: list[Account]) -> str:
    """The whole password file. Mosquitto 2.0 dropped plain-text password
    files, so every line carries a hash and the plaintext exists only in
    `accounts.json` and SecretStore."""
    lines = [
        f"{account.username}:{password_hash(account.password, os.urandom(PW_SALT_BYTES))}"
        for account in accounts
    ]
    return "".join(f"{line}\n" for line in lines)


# --- Mosquitto ACL file -----------------------------------------------------


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

    **Why this is a function and not two literal blocks.** Two authorization
    backends now render these grants — the dev broker's `acl_file` (E3.1) and
    the dynamic security role each minted credential holds (E5.6) — and two
    literal readings of the same spec table will eventually disagree. The
    disagreement that matters is a single missing line: an Aggregator that may
    publish to its own `desired` topic can manufacture agreement with itself
    and defeat drift detection entirely. So both renderers read THIS, and a
    test asserts both against it (phase-5 E5.6 acceptance).

    E5.8a moves this to `app/brokerconfig.py` along with the rest of the
    broker material; it lives here now because `acl_file_text` is its first
    caller and a move is a separate, provable step.
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


def acl_file_text(accounts: list[Account]) -> str:
    """The ACL file. With `acl_file` set, Mosquitto denies every topic it was
    not explicitly granted, so this file IS the isolation guarantee of spec
    7.1 — the device grants come from `aggregator_acl_grants`, which is the
    spec 7.2 table's Direction column read literally, and which the dynsec
    role of every minted credential is rendered from as well (E5.6).
    """
    blocks: list[str] = [
        "# GENERATED by app.devbroker (task E3.1) - do not edit by hand.",
        "# Mosquitto denies any topic not granted here.",
        "",
    ]
    for account in accounts:
        if account.kind == "platform":
            blocks += [
                "# platform account: broker-wide inside one deployment (spec 7.1)",
                f"user {account.username}",
                f"topic readwrite {deployment_root(account.deployment_slug)}/#",
                "",
            ]
            continue
        assert account.aggregator_uuid is not None
        blocks += [
            "# aggregator: its own subtree only, one grant per spec 7.2 row",
            f"user {account.username}",
            *[
                f"topic {grant.access} {grant.topic}"
                for grant in aggregator_acl_grants(account.deployment_slug, account.aggregator_uuid)
            ],
            "",
        ]
    return "\n".join(blocks)


# --- TLS material -----------------------------------------------------------


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_private(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def generate_tls_material() -> dict[str, bytes]:
    """A private CA and one server certificate signed by it. Both are
    short-lived dev artifacts; the platform trusts the CA (stored on the
    deployment_service row) rather than disabling verification, so the TLS
    path is genuinely exercised instead of merely configured."""
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
    alt_names: list[x509.GeneralName] = [x509.DNSName(host) for host in CERT_HOSTNAMES]
    alt_names += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in CERT_IPS]
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_HOSTNAMES[0])]))
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


# --- Writing the artifact directory ----------------------------------------


def write_artifacts(
    out_dir: Path,
    accounts: list[Account],
    *,
    host: str,
    port: int,
    regenerate_tls: bool,
) -> None:
    """Write (or refresh) the whole dev-certs directory.

    The directory and the private keys land world-readable on purpose: the
    Mosquitto container drops to uid 1883 and can neither traverse a 0700
    directory nor read a 0600 file that arrived from the host. These keys
    protect nothing — they are generated, gitignored, and rotated on every run
    — and the alternative is an init container whose only job is a chown."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        out_dir.chmod(0o755)
    except PermissionError as error:
        # Docker creates a missing bind-mount source directory as root. If the
        # stack came up before this ever ran, `deploy/dev-certs` exists but
        # belongs to root and nothing here can write into it. Say so plainly:
        # the raw errno is a genuinely baffling way to learn this.
        raise SystemExit(
            f"cannot write to {out_dir}: it exists but is not owned by you.\n"
            "Docker created it as root when the stack came up before the "
            "certificates existed. Remove it and run this again:\n"
            f'  docker run --rm -v "{out_dir.parent}:/work" alpine:3 '
            f"rm -rf /work/{out_dir.name}"
        ) from error
    if regenerate_tls or not (out_dir / "ca.crt").is_file():
        for name, blob in generate_tls_material().items():
            path = out_dir / name
            path.write_bytes(blob)
            path.chmod(0o644)

    (out_dir / "passwd").write_text(password_file_text(accounts), encoding="utf-8")
    (out_dir / "acl").write_text(acl_file_text(accounts), encoding="utf-8")
    (out_dir / "passwd").chmod(0o644)
    (out_dir / "acl").chmod(0o644)

    manifest = {
        "host": host,
        "port": port,
        "ca_cert": str((out_dir / "ca.crt").resolve()),
        "accounts": [asdict(account) for account in accounts],
    }
    (out_dir / "accounts.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def load_manifest(out_dir: Path = DEV_CERTS) -> dict[str, Any]:
    """Read back what the last run wrote — how the test suite and the mock
    device learn their credentials without a second source of truth."""
    manifest: dict[str, Any] = json.loads((out_dir / "accounts.json").read_text(encoding="utf-8"))
    return manifest


# --- Database side ----------------------------------------------------------


def plan_accounts(db: Session) -> list[Account]:
    """One platform account per deployment plus one device account per
    Aggregator, with freshly generated passwords."""
    accounts: list[Account] = []
    for slug in db.scalars(select(Deployment.slug).order_by(Deployment.slug)):
        accounts.append(
            Account(
                username=platform_username(slug),
                password=secrets.token_urlsafe(24),
                kind="platform",
                deployment_slug=slug,
            )
        )
    rows = db.execute(
        select(Deployment.slug, Aggregator.aggregator_uuid)
        .join(Pod, Pod.deployment_id == Deployment.id)
        .join(Aggregator, Aggregator.pod_id == Pod.id)
        .order_by(Deployment.slug, Aggregator.aggregator_uuid)
    )
    for slug, aggregator_uuid in rows:
        accounts.append(
            Account(
                username=device_username(aggregator_uuid),
                password=secrets.token_urlsafe(24),
                kind="device",
                deployment_slug=slug,
                aggregator_uuid=aggregator_uuid,
            )
        )
    return accounts


def register_services(
    db: Session,
    secret_store: SecretStore,
    accounts: list[Account],
    *,
    host: str,
    port: int,
    ca_cert_pem: str,
) -> int:
    """Upsert the `mqtt` deployment_service row for every deployment that got
    a platform account, putting the password through SecretStore and never
    into the row (rule R2). Returns the number of rows written."""
    by_slug = {a.deployment_slug: a for a in accounts if a.kind == "platform"}
    written = 0
    for deployment in db.scalars(select(Deployment).order_by(Deployment.slug)):
        account = by_slug.get(deployment.slug)
        if account is None:
            continue
        row = db.scalar(
            select(DeploymentService).where(
                DeploymentService.deployment_id == deployment.id,
                DeploymentService.service_key == "mqtt",
            )
        )
        if row is None:
            row = DeploymentService(deployment_id=deployment.id, service_key="mqtt")
            db.add(row)
        row.host = host
        row.port = port
        row.tls_enabled = True
        row.ca_cert_pem = ca_cert_pem
        row.username = account.username
        row.password_secret_name = secret_name(deployment.id)
        secret_store.put(row.password_secret_name, account.password)
        written += 1
    db.commit()
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the development MQTT broker (E3.1)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="broker host the PLATFORM dials")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--out", type=Path, default=DEV_CERTS)
    parser.add_argument(
        "--certs-only",
        action="store_true",
        help="TLS material and empty account files only; touches no database",
    )
    parser.add_argument(
        "--keep-tls",
        action="store_true",
        help="reuse existing certificates instead of regenerating them",
    )
    args = parser.parse_args(argv)

    if args.certs_only:
        write_artifacts(args.out, [], host=args.host, port=args.port, regenerate_tls=True)
        print(f"dev broker TLS material written to {args.out} (no accounts yet)")
        return 0

    settings = Settings()  # type: ignore[call-arg]
    _, session_factory = create_session_factory(settings.database_url)
    secret_store = SecretStore(session_factory, settings.kek)
    with session_factory() as db:
        accounts = plan_accounts(db)
        if not accounts:
            print("no deployments found; seed one first (uv run python -m app.seed --demo)")
            return 1
        write_artifacts(
            args.out,
            accounts,
            host=args.host,
            port=args.port,
            regenerate_tls=not args.keep_tls,
        )
        ca_cert_pem = (args.out / "ca.crt").read_text(encoding="utf-8")
        written = register_services(
            db,
            secret_store,
            accounts,
            host=args.host,
            port=args.port,
            ca_cert_pem=ca_cert_pem,
        )

    platform = sum(1 for a in accounts if a.kind == "platform")
    devices = len(accounts) - platform
    print(
        f"dev broker provisioned in {args.out}: {platform} platform account(s), "
        f"{devices} device account(s), {written} deployment_service row(s)"
    )
    print("restart the broker so it reloads passwd/acl:  docker compose restart mosquitto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
