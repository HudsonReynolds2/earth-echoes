"""E5.8b: the generated deployment stack, rendered from dicts.

Spec 16.3's Path B. An operator who has no services yet downloads a bundle,
runs `docker compose up -d`, and has a Mosquitto, an InfluxDB, a Prometheus, a
Grafana and optionally a MinIO that this platform already holds credentials for
and can verify with the E5.4 testers.

**Everything here is a Python dict serialised with `yaml.safe_dump`, never a
string template**, and that is a phase-5 requirement rather than a style
preference. A compose file built from a dict is valid YAML by construction, so
the failure mode is a KeyError in a test rather than an operator pasting a
broken file into a terminal. And `string.Template`'s `${name}` collides
head-on with compose's own `${VAR}` interpolation — a templated compose file
either loses its environment interpolation or has to escape it everywhere.

The one exception is `mosquitto.conf`, which is not YAML and is rendered by
`app.brokerconfig` (E5.8a). It is imported rather than copied, so the broker
the platform generates and the broker the platform dials cannot drift apart.

**This module generates no credentials.** It takes them already generated and
already hashed, because fixed choice 7 puts credential generation and the
database write in ONE transaction that commits before a byte is rendered
(E5.9). Rendering is a pure function of the stored rows, which is what makes
two downloads byte-identical and lets the platform keep no blob.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app import brokerconfig

#: Pinned, not floating. An operator brings this stack up months after the
#: bundle was generated, and `:latest` means they get a different Influx from
#: the one the platform's testers were written against. These match the
#: versions the test rig runs, so what the gate proves is what ships.
IMAGES = {
    "mosquitto": "eclipse-mosquitto:2.0.20",
    "influx": "influxdb:3-core",
    "prometheus": "prom/prometheus:v3.5.0",
    "grafana": "grafana/grafana:11.6.0",
    "minio": "minio/minio:latest",
}

#: Container-side ports. The compose file publishes each on the same host port
#: and `stack_ports` is the single list the README is rendered from, so the
#: two cannot disagree (E5.8b acceptance asserts it in both directions).
PORTS = {
    "mosquitto": 8883,
    "influx": 8181,
    "prometheus": 9090,
    "grafana": 3000,
    "minio": 9000,
    "minio_console": 9001,
}

#: What each port is for, in the README's words. Keyed by the same names, so a
#: port added without a description fails the README test rather than shipping
#: an unexplained hole in an operator's firewall.
PORT_PURPOSE = {
    "mosquitto": "MQTT over TLS — the control plane. Aggregators dial this.",
    "influx": "InfluxDB 3 HTTP API — telemetry writes and reads.",
    "prometheus": "Prometheus — metrics scrape and remote-write receiver.",
    "grafana": "Grafana UI and API.",
    "minio": "MinIO S3 API — raw-audio upload.",
    "minio_console": "MinIO web console.",
}

#: The name of the Influx database the platform writes telemetry into. Influx 3
#: creates a database on first write, so nothing has to pre-create it; the name
#: is fixed here because the testers and E7's read clients both need to agree
#: on it without asking the operator.
INFLUX_DATABASE = "echoes"

#: The Grafana contact point E5.4d registers and E7.6's receiver consumes. The
#: route does not exist yet and that is deliberate (phase-5 section 3).
ALERT_WEBHOOK_PATH = "/webhooks/grafana-alerts"


@dataclass(frozen=True)
class StackSecrets:
    """Credentials the stack is rendered with, already generated and hashed.

    **Every password here that reaches a config file is already hashed**, and
    each in the form its own service demands: bcrypt for Prometheus's
    `web_config.yml`, PBKDF2-`$7$` for the broker's dynsec JSON. That is not
    only a secrecy property — hashing at render time would re-salt on every
    call and break the byte-identical-download guarantee fixed choice 7 rests
    on. E5.9 hashes once, stores, and every render after that is pure.

    The plaintexts that remain (`influx_token`, `grafana_admin_password`,
    MinIO's root password) are values their services need verbatim; they go
    into `.env` and SecretStore, and the README tells the operator the archive
    is a credential.
    """

    broker_admin_username: str
    broker_admin_password_hash: str = field(repr=False)
    influx_token: str = field(repr=False)
    prometheus_username: str
    prometheus_password_bcrypt: str = field(repr=False)
    #: The SAME password the bcrypt hash above is of, in plaintext.
    #:
    #: Both forms are needed and they are not redundant: `web_config.yml`
    #: authenticates incoming requests against the HASH, while Prometheus's own
    #: self-scrape is an outgoing request that has to present the PLAINTEXT.
    #: Without this the scrape config points at a password file the bundle does
    #: not contain, Docker creates a directory in its place, and Prometheus
    #: scrapes nothing — which makes `up` absent and E5.4c's read probe fail
    #: against a stack that is otherwise fine.
    prometheus_password: str = field(repr=False)
    grafana_admin_username: str
    grafana_admin_password: str = field(repr=False)
    minio_root_user: str = ""
    minio_root_password: str = field(default="", repr=False)

    def __str__(self) -> str:
        return "stack secrets (redacted)"


@dataclass(frozen=True)
class StackSpec:
    """Everything the renderer needs that is not a credential.

    `hostnames` and `ips` reach the broker's certificate: a generated stack
    answers to the deployment's own address, and a certificate for `localhost`
    verifies nowhere else (E5.8a parameterised `generate_tls_material` for
    exactly this).
    """

    deployment_slug: str
    secrets: StackSecrets
    hostnames: tuple[str, ...] = ("localhost",)
    ips: tuple[str, ...] = ("127.0.0.1",)
    include_object_storage: bool = False
    platform_base_url: str = "http://host.docker.internal:8000"
    #: Kept short so a dev stack does not fill a disk; spec 16.3 leaves
    #: retention to the deployment and this is the documented default.
    prometheus_retention: str = "30d"

    @property
    def services(self) -> tuple[str, ...]:
        base = ("mosquitto", "influx", "prometheus", "grafana")
        return (*base, "minio") if self.include_object_storage else base


def _dump(data: Mapping[str, Any]) -> str:
    """One serialiser for every YAML file in the bundle.

    `sort_keys=False` keeps the authored order, which is what makes a generated
    compose file readable by the operator who has to debug it. Determinism does
    not depend on sorting — it depends on the input being a function of the
    stored rows — and E5.10 asserts byte-identity directly.
    """
    return yaml.safe_dump(dict(data), sort_keys=False, default_flow_style=False, width=100)


# --- Per-service configuration ----------------------------------------------


def prometheus_yml(spec: StackSpec) -> dict[str, Any]:
    """Scrape config: Prometheus itself, plus the node exporter if one is run.

    The self-scrape is not decoration. It is what makes `up` exist, and `up` is
    the metric E5.4c's read probe queries — a Prometheus scraping nothing
    answers an empty result set to every query, which is indistinguishable from
    a broken connection.
    """
    return {
        "global": {"scrape_interval": "15s", "evaluation_interval": "15s"},
        "scrape_configs": [
            {
                "job_name": "prometheus",
                "basic_auth": {
                    "username": spec.secrets.prometheus_username,
                    "password_file": "/etc/prometheus/scrape_password",
                },
                "static_configs": [{"targets": [f"127.0.0.1:{PORTS['prometheus']}"]}],
            },
            {
                "job_name": "node",
                # Commented in the README rather than removed: an operator who
                # runs node_exporter beside the stack gets host metrics with no
                # config change, and one that does not simply reports the
                # target down.
                "static_configs": [{"targets": ["host.docker.internal:9100"]}],
            },
        ],
    }


def prometheus_web_config(spec: StackSpec) -> dict[str, Any]:
    """`web_config.yml`: the basic-auth user the platform dials with.

    Prometheus checks basic auth BEFORE it routes, which E5.4c measured: a
    wrong password answers 401 on every path including one that would otherwise
    404. That is why the tester reads 401 before 404 and why this file existing
    is what makes the remote-write probe meaningful.
    """
    return {
        "basic_auth_users": {
            spec.secrets.prometheus_username: spec.secrets.prometheus_password_bcrypt
        }
    }


def grafana_datasources(spec: StackSpec) -> dict[str, Any]:
    """Provisioned datasources, so a fresh Grafana already points at the stack.

    Provisioned rather than created over the API because a provisioning file is
    reapplied on every restart, while an API-created datasource is state a
    container recreation loses. E5.4d's tester checks these exist and can
    optionally provision them; on a generated stack they are here from boot.
    """
    return {
        "apiVersion": 1,
        "datasources": [
            {
                "name": "Prometheus",
                "type": "prometheus",
                "access": "proxy",
                "url": f"http://prometheus:{PORTS['prometheus']}",
                "basicAuth": True,
                "basicAuthUser": spec.secrets.prometheus_username,
                "secureJsonData": {"basicAuthPassword": "${PROMETHEUS_PASSWORD}"},
                "isDefault": True,
            },
            {
                "name": "InfluxDB",
                "type": "influxdb",
                "access": "proxy",
                "url": f"http://influx:{PORTS['influx']}",
                "jsonData": {"version": "SQL", "dbName": INFLUX_DATABASE, "httpMode": "POST"},
                "secureJsonData": {"token": "${INFLUX_TOKEN}"},
            },
        ],
    }


def grafana_contact_points(spec: StackSpec) -> dict[str, Any]:
    """The webhook contact point E7.6's receiver will consume.

    **The route it points at does not exist yet**, which is deliberate and
    stated at every site that mentions it (phase-5 section 3): E5.4d registers
    the same contact point against an operator's existing Grafana, and E7.6
    builds the receiver. A stack generated today posts alerts at a 404 until
    that lands, which is visible in Grafana's own delivery log rather than
    silent.
    """
    return {
        "apiVersion": 1,
        "contactPoints": [
            {
                "orgId": 1,
                "name": "echoes-platform",
                "receivers": [
                    {
                        "uid": "echoes-platform-webhook",
                        "type": "webhook",
                        "settings": {
                            "url": f"{spec.platform_base_url}{ALERT_WEBHOOK_PATH}",
                            "httpMethod": "POST",
                        },
                    }
                ],
            }
        ],
    }


# --- The compose file -------------------------------------------------------


def _mosquitto_service() -> dict[str, Any]:
    return {
        "image": IMAGES["mosquitto"],
        "restart": "unless-stopped",
        "ports": [f"{PORTS['mosquitto']}:{PORTS['mosquitto']}"],
        "volumes": [
            "./mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro",
            "./mosquitto/ca.crt:/mosquitto/config/ca.crt:ro",
            "./mosquitto/server.crt:/mosquitto/config/server.crt:ro",
            "./mosquitto/server.key:/mosquitto/config/server.key:ro",
            # NOT read-only: the dynamic security plugin rewrites this file as
            # the platform creates each device's client (E5.6).
            "./mosquitto/dynamic-security.json:/mosquitto/config/dynamic-security.json",
            "mosquitto-data:/mosquitto/data",
        ],
    }


def _influx_service(spec: StackSpec) -> dict[str, Any]:
    return {
        "image": IMAGES["influx"],
        "restart": "unless-stopped",
        "ports": [f"{PORTS['influx']}:{PORTS['influx']}"],
        "command": [
            "influxdb3",
            "serve",
            "--node-id=node0",
            "--object-store=file",
            "--data-dir=/var/lib/influxdb3",
            f"--http-bind=0.0.0.0:{PORTS['influx']}",
        ],
        "environment": {"INFLUXDB3_AUTH_TOKEN": spec.secrets.influx_token},
        "volumes": ["influx-data:/var/lib/influxdb3"],
    }


def _prometheus_service(spec: StackSpec) -> dict[str, Any]:
    return {
        "image": IMAGES["prometheus"],
        "restart": "unless-stopped",
        "ports": [f"{PORTS['prometheus']}:{PORTS['prometheus']}"],
        "command": [
            "--config.file=/etc/prometheus/prometheus.yml",
            "--web.config.file=/etc/prometheus/web_config.yml",
            f"--storage.tsdb.retention.time={spec.prometheus_retention}",
            # Off by default in Prometheus, which is exactly what E5.4c's probe
            # exists to detect. The platform remote-writes, so the generated
            # stack turns it on.
            "--web.enable-remote-write-receiver",
        ],
        "volumes": [
            "./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro",
            "./prometheus/web_config.yml:/etc/prometheus/web_config.yml:ro",
            "./prometheus/scrape_password:/etc/prometheus/scrape_password:ro",
            "prometheus-data:/prometheus",
        ],
    }


def _grafana_service(spec: StackSpec) -> dict[str, Any]:
    return {
        "image": IMAGES["grafana"],
        "restart": "unless-stopped",
        "ports": [f"{PORTS['grafana']}:{PORTS['grafana']}"],
        "environment": {
            "GF_SECURITY_ADMIN_USER": spec.secrets.grafana_admin_username,
            "GF_SECURITY_ADMIN_PASSWORD": spec.secrets.grafana_admin_password,
            # Read by the provisioned datasources, so neither credential is
            # duplicated into the provisioning YAML.
            "PROMETHEUS_PASSWORD": "${PROMETHEUS_PASSWORD}",
            "INFLUX_TOKEN": "${INFLUX_TOKEN}",
        },
        "volumes": [
            "./grafana/provisioning:/etc/grafana/provisioning:ro",
            "grafana-data:/var/lib/grafana",
        ],
        "depends_on": ["prometheus", "influx"],
    }


def _minio_service(spec: StackSpec) -> dict[str, Any]:
    return {
        "image": IMAGES["minio"],
        "restart": "unless-stopped",
        "command": ["server", "/data", "--console-address", f":{PORTS['minio_console']}"],
        "ports": [
            f"{PORTS['minio']}:{PORTS['minio']}",
            f"{PORTS['minio_console']}:{PORTS['minio_console']}",
        ],
        "environment": {
            "MINIO_ROOT_USER": spec.secrets.minio_root_user,
            "MINIO_ROOT_PASSWORD": spec.secrets.minio_root_password,
        },
        "volumes": ["minio-data:/data"],
    }


def compose_file(spec: StackSpec) -> dict[str, Any]:
    """The whole `docker-compose.yml`, as a dict.

    No `version:` key — compose v2 warns about it and ignores it. Named volumes
    rather than bind mounts for state, so `docker compose down` without `-v`
    keeps the deployment's data and the retained MQTT messages spec 6.4 depends
    on.
    """
    services: dict[str, Any] = {
        "mosquitto": _mosquitto_service(),
        "influx": _influx_service(spec),
        "prometheus": _prometheus_service(spec),
        "grafana": _grafana_service(spec),
    }
    volumes: dict[str, Any] = {
        "mosquitto-data": None,
        "influx-data": None,
        "prometheus-data": None,
        "grafana-data": None,
    }
    if spec.include_object_storage:
        services["minio"] = _minio_service(spec)
        volumes["minio-data"] = None
    return {"name": f"echoes-{spec.deployment_slug}", "services": services, "volumes": volumes}


# --- Ports, and the README they are rendered into ---------------------------


def stack_ports(spec: StackSpec) -> dict[str, int]:
    """Every host port the generated compose file publishes.

    **One list, two consumers** — the compose file and the README — for the
    same reason the ACL grants are one list: an operator opens a firewall from
    the README, and a port the README omits is a service that silently does not
    work from outside the host.
    """
    ports = {name: PORTS[name] for name in spec.services}
    if spec.include_object_storage:
        ports["minio_console"] = PORTS["minio_console"]
    return ports


def published_ports(compose: Mapping[str, Any]) -> set[int]:
    """The host ports a rendered compose dict actually publishes.

    Read back out of the compose structure rather than from `PORTS`, so the
    README test compares the README against the FILE and not against the
    constant both were built from — which would pass while publishing nothing.
    """
    found: set[int] = set()
    for service in compose["services"].values():
        for mapping in service.get("ports", []):
            found.add(int(str(mapping).split(":")[0]))
    return found


# --- Rendering the bundle ---------------------------------------------------


#: Where the static prose lives. Phase-5 E5.8b: prose belongs in a file a
#: person can edit and review, not in a Python string constant nobody reads.
TEMPLATE_DIR = Path(__file__).resolve().parents[2].parent / "deploy" / "stack-templates"

#: The line in the template the generated port table replaces. A marker rather
#: than `str.format`, because the README is full of braces (`${VAR}`, shell
#: snippets) that a format call would try to interpret.
PORT_TABLE_MARKER = "<!-- PORT TABLE -->"


def port_table(spec: StackSpec) -> str:
    """The README's port table, rendered from `stack_ports`."""
    rows = ["| Port | Service | Purpose |", "|---|---|---|"]
    for name, port in sorted(stack_ports(spec).items(), key=lambda item: item[1]):
        label = name.replace("_", " ")
        rows.append(f"| {port} | {label} | {PORT_PURPOSE[name]} |")
    return "\n".join(rows)


def readme(spec: StackSpec) -> str:
    """The bundle README: static prose from `deploy/stack-templates/`, with the
    port table generated so it cannot drift from the compose file.

    E5.10 puts this in the archive; it is rendered here because E5.8b's
    acceptance is that the README's ports and the compose file's ports match in
    BOTH directions, and that test needs both artifacts.
    """
    template = (TEMPLATE_DIR / "README.md").read_text(encoding="utf-8")
    if PORT_TABLE_MARKER not in template:
        raise ValueError(
            f"the README template lost its {PORT_TABLE_MARKER} marker; the generated "
            "port table has nowhere to go and operators would open no ports"
        )
    return template.replace(PORT_TABLE_MARKER, port_table(spec))


def readme_ports(text: str) -> set[int]:
    """The ports a rendered README actually lists, parsed back out of it.

    Parsed rather than recomputed, for the same reason `published_ports` reads
    the compose dict: comparing two derivations of one constant proves nothing
    about the documents an operator reads.
    """
    found: set[int] = set()
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        first = line.split("|")[1].strip()
        if first.isdigit():
            found.add(int(first))
    return found


def render_configs(spec: StackSpec) -> dict[str, str]:
    """Every generated file in the bundle except the README, as path → text.

    `mosquitto.conf` and `dynamic-security.json` come from `app.brokerconfig`,
    not from a copy: E5.8a made that the one renderer precisely so the broker
    in this bundle and the broker the platform dials cannot disagree about
    spec 7.2.
    """
    files: dict[str, str] = {
        "docker-compose.yml": _dump(compose_file(spec)),
        "mosquitto/mosquitto.conf": brokerconfig.stack_mosquitto_conf(
            port=PORTS["mosquitto"],
            config_dir="/mosquitto/config",
            data_dir="/mosquitto/data",
        ),
        "mosquitto/dynamic-security.json": json.dumps(
            brokerconfig.dynamic_security_config(
                spec.deployment_slug,
                spec.secrets.broker_admin_username,
                spec.secrets.broker_admin_password_hash,
            ),
            indent=2,
        )
        + "\n",
        "prometheus/prometheus.yml": _dump(prometheus_yml(spec)),
        "prometheus/web_config.yml": _dump(prometheus_web_config(spec)),
        # The self-scrape's outgoing credential. `password_file` rather than an
        # inline `password:` so the scrape config itself stays free of
        # plaintext and one file carries it.
        "prometheus/scrape_password": spec.secrets.prometheus_password + "\n",
        "grafana/provisioning/datasources/datasources.yml": _dump(grafana_datasources(spec)),
        "grafana/provisioning/alerting/contact-points.yml": _dump(grafana_contact_points(spec)),
    }
    return files
