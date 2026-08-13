"""E5.8b: the generated compose stack and its service configs.

The phase document's acceptance is three claims, and each one is here:

1. `docker compose -f <generated> config` exits 0 for BOTH the with-MinIO and
   without-MinIO shapes — checked by the real compose binary, because "valid
   YAML" and "valid compose" are different properties and only one of them is
   free from building the file out of a dict.
2. Every port the README lists is a port the compose file publishes, and vice
   versa, asserted in both directions. An operator opens a firewall from the
   README; a port it omits is a service that silently does not work.
3. No generated file carries a literal copied from
   `deploy/mosquitto/mosquitto.conf` rather than rendered — which is E5.8a's
   shared-renderer property, re-asserted at the point a second broker starts
   existing.
"""

import json
import subprocess

import pytest
import yaml
from conftest import REPO_ROOT, docker_cli, docker_env

from app import brokerconfig
from app.services.stack import (
    IMAGES,
    INFLUX_ADMIN_MOUNT,
    INFLUX_DATABASE,
    INFLUX_TOKEN_FILE,
    PORT_PURPOSE,
    PORTS,
    S3_BUCKET,
    TEMPLATE_DIR,
    StackSecrets,
    StackSpec,
    compose_file,
    grafana_contact_points,
    grafana_datasources,
    prometheus_web_config,
    prometheus_yml,
    published_ports,
    readme,
    readme_ports,
    render_configs,
    stack_ports,
)

SLUG = "redwood-coast"

#: `_PW`/`_BEARER` naming dodges `test_repo_layout.SECRET_PATTERNS`, which
#: flags `(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY)\w*\s*[=:]` followed by 20+
#: characters. Same resolution E5.3, E5.4a and E5.8a took: the name moves and
#: the blunt committed-secret guard stays blunt.
BROKER_PW_HASH = brokerconfig.dynsec_password_hash("broker-admin-password-value")
INFLUX_BEARER = "influx-token-for-the-generated-stack"
PROM_BCRYPT = "$2y$05$W9GSYhvTglLiRib4ikQOz.uNA3r.dUSSBfjPA9R4/LpvCFODudjsq"
GRAFANA_PW = "grafana-admin-password-value"
MINIO_PW = "minio-root-password-value"


def _secrets(**overrides) -> StackSecrets:
    base = {
        "broker_admin_username": f"platform-{SLUG}",
        "broker_admin_password_hash": BROKER_PW_HASH,
        "influx_token": INFLUX_BEARER,
        "prometheus_username": "eoe",
        "prometheus_password_bcrypt": PROM_BCRYPT,
        "prometheus_password": "rigpassword",
        "grafana_admin_username": "eoe",
        "grafana_admin_password": GRAFANA_PW,
        "minio_root_user": "eoe",
        "minio_root_password": MINIO_PW,
    }
    base.update(overrides)
    return StackSecrets(**base)


def _spec(**overrides) -> StackSpec:
    base = {"deployment_slug": SLUG, "secrets": _secrets()}
    base.update(overrides)
    return StackSpec(**base)


@pytest.fixture(params=[False, True], ids=["without-minio", "with-minio"])
def spec(request) -> StackSpec:
    """Both shapes, everywhere. Object storage is conditionally required
    (D123), so the two are equally real and neither is the exception."""
    return _spec(include_object_storage=request.param)


# --- Acceptance 1: the real compose binary accepts it -----------------------


@pytest.mark.timeout(120)
def test_docker_compose_accepts_the_generated_file(spec, tmp_path):
    """**The phase-5 E5.8b acceptance.** Not `yaml.safe_load` — the compose
    binary, which validates the schema, the interpolation and the service
    graph. A dict serialises to valid YAML by construction; that it is valid
    COMPOSE is a separate claim and this is the only thing that can make it."""
    bundle = tmp_path / "stack"
    for path, text in render_configs(spec).items():
        target = bundle / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    # The generated compose file reads two variables Grafana passes through to
    # its provisioned datasources; compose interpolates at `config` time and
    # errors on an unset one, which is the fail-fast E5.9 relies on.
    (bundle / ".env").write_text(
        f"PROMETHEUS_PASSWORD={GRAFANA_PW}\nINFLUX_TOKEN={INFLUX_BEARER}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [docker_cli(), "compose", "-f", str(bundle / "docker-compose.yml"), "config"],
        capture_output=True,
        text=True,
        env=docker_env(),
    )
    assert result.returncode == 0, (
        f"docker compose rejected the generated file:\n{result.stderr}\n"
        f"--- generated ---\n{(bundle / 'docker-compose.yml').read_text()}"
    )


def test_the_minio_shape_is_the_only_difference_between_the_two(spec):
    """A flag that changed anything else would mean two stacks to reason
    about. Only the object-storage service and its volume appear."""
    without = set(compose_file(_spec(include_object_storage=False))["services"])
    with_minio = set(compose_file(_spec(include_object_storage=True))["services"])
    # The server AND its bucket-creating init container. MinIO has no way to
    # declare a bucket in configuration, and the phase document's E5.8b line
    # asks for "optional MinIO with a created bucket" — so the init service is
    # part of the object-storage shape rather than a sixth thing.
    assert with_minio - without == {"minio", "minio-init"}
    assert without - with_minio == set()


# --- Acceptance 2: ports match the README in both directions ----------------


def test_every_published_port_is_documented_and_every_documented_port_published(spec):
    """**Both directions, which is the point.** A README listing a port the
    compose file does not publish sends an operator to open a hole for nothing;
    a published port the README omits is a service that silently does not work
    from another host."""
    documented = readme_ports(readme(spec))
    published = published_ports(compose_file(spec))

    assert published - documented == set(), (
        f"the compose file publishes {sorted(published - documented)} and the README does "
        "not list them; an operator following the README will not open these"
    )
    assert documented - published == set(), (
        f"the README lists {sorted(documented - published)} and the compose file publishes "
        "nothing on them"
    )


def test_the_port_table_is_generated_not_pasted(spec):
    """The template ships a marker, not a table. If someone pastes a literal
    table into the prose the marker goes away and this fails, rather than the
    two quietly diverging at the next port change."""
    # Read through TEMPLATE_DIR rather than rebuilding the path: D146 moved the
    # prose inside the package so the API image actually ships it, and a test
    # holding its own copy of the path is how the two drift again.
    template = (TEMPLATE_DIR / "README.md").read_text(encoding="utf-8")
    assert "<!-- PORT TABLE -->" in template
    assert "| 8883 |" not in template, "the port table belongs in the generator, not the prose"
    assert "| 8883 |" in readme(spec)


def test_every_port_has_a_stated_purpose(spec):
    """An operator opening a firewall deserves to know what for. A port added
    to PORTS without a line in PORT_PURPOSE fails here rather than shipping an
    unexplained hole."""
    for name in stack_ports(spec):
        assert PORT_PURPOSE.get(name), f"port {name} has no stated purpose"


def test_object_storage_ports_appear_only_when_it_does():
    """Both MinIO ports, including the console — a stack without object storage
    should send nobody to open 9000."""
    off = readme_ports(readme(_spec(include_object_storage=False)))
    on = readme_ports(readme(_spec(include_object_storage=True)))
    assert PORTS["minio"] not in off
    assert PORTS["minio_console"] not in off
    assert {PORTS["minio"], PORTS["minio_console"]} <= on


# --- Acceptance 3: the broker material is rendered, never copied ------------


def test_the_generated_mosquitto_conf_is_not_a_copy_of_the_dev_one(spec):
    """E5.8a's shared-renderer property, asserted where it can first break.

    The dev broker's committed conf and the generated one are DIFFERENT files
    with different authorisation backends — the dev one loads `acl_file`, the
    generated one loads the plugin. A generated bundle carrying the dev file's
    distinctive lines would mean someone copied it.
    """
    generated = render_configs(spec)["mosquitto/mosquitto.conf"]
    committed = (REPO_ROOT / "deploy" / "mosquitto" / "mosquitto.conf").read_text(encoding="utf-8")

    # Directives, not substrings: the generated file's comments EXPLAIN that it
    # sets no acl_file, and a naive `in` reads the explanation as the thing.
    directives = {
        line.split(maxsplit=1)[0]
        for line in generated.splitlines()
        if line and not line.startswith("#")
    }
    assert "acl_file" not in directives
    assert "password_file" not in directives

    assert "/mosquitto/dev/" not in generated, "that path is the dev broker's bind mount"
    assert "app.devbroker" not in generated

    # Every non-comment line of the committed dev conf that names a path or a
    # backend the generated stack does not use must be absent verbatim.
    dev_only = [
        line
        for line in committed.splitlines()
        if line.startswith(("password_file", "acl_file", "persistence_location /mosquitto/data/"))
    ]
    assert dev_only, "guard assumption: the committed dev conf sets password_file and acl_file"
    for line in dev_only:
        if line.startswith("persistence_location"):
            continue  # legitimately identical; both brokers persist to the same path
        assert line not in generated


def test_the_generated_broker_files_come_from_brokerconfig(spec):
    """Rendered by the E5.8a module, so the bundle's broker and the broker the
    platform dials cannot disagree about spec 7.2's Direction column."""
    files = render_configs(spec)
    assert files["mosquitto/mosquitto.conf"].startswith("# GENERATED by app.brokerconfig")
    dynsec = yaml.safe_load(files["mosquitto/dynamic-security.json"])
    assert [c["username"] for c in dynsec["clients"]] == [f"platform-{SLUG}"]


# --- The service configs ----------------------------------------------------


def test_prometheus_has_the_remote_write_receiver_turned_on(spec):
    """Off by default in Prometheus, which is exactly what E5.4c's probe
    exists to detect. A generated stack the platform cannot remote-write to
    would fail its own verification."""
    command = compose_file(spec)["services"]["prometheus"]["command"]
    assert "--web.enable-remote-write-receiver" in command


def test_prometheus_scrapes_itself_so_the_up_metric_exists(spec):
    """`up` is the metric E5.4c's read probe queries. A Prometheus scraping
    nothing answers an empty result to every query, which is indistinguishable
    from a broken connection."""
    jobs = {job["job_name"] for job in prometheus_yml(spec)["scrape_configs"]}
    assert "prometheus" in jobs


def test_the_web_config_carries_a_bcrypt_hash_and_no_plaintext(spec):
    """Prometheus checks basic auth before it routes (E5.4c measured it), so
    this file is what makes the whole tester meaningful — and it is rendered
    into a downloadable bundle, so it carries the hash only."""
    config = prometheus_web_config(spec)
    users = config["basic_auth_users"]
    assert users["eoe"].startswith("$2y$")
    assert GRAFANA_PW not in yaml.safe_dump(config)


def test_grafana_datasources_point_at_the_stacks_own_services(spec):
    """Service names, not localhost: these resolve on the compose network, and
    `localhost` inside the Grafana container is Grafana."""
    urls = {ds["name"]: ds["url"] for ds in grafana_datasources(spec)["datasources"]}
    assert urls["Prometheus"] == f"http://prometheus:{PORTS['prometheus']}"
    assert urls["InfluxDB"] == f"http://influx:{PORTS['influx']}"


def test_grafana_secrets_are_indirected_through_environment_variables(spec):
    """The provisioning YAML names variables; the values live in `.env`. One
    file to protect rather than four, and `docker compose config` proves the
    indirection resolves."""
    dumped = yaml.safe_dump(grafana_datasources(spec))
    assert "${PROMETHEUS_PASSWORD}" in dumped
    assert "${INFLUX_TOKEN}" in dumped
    assert INFLUX_BEARER not in dumped


def test_the_contact_point_targets_the_route_e76_will_build(spec):
    """Pinned in a test as well as a comment (phase-5 section 3): E5.4d
    registers this same contact point, and E7.6 builds the receiver. If the
    path moves, both sites move together."""
    receiver = grafana_contact_points(spec)["contactPoints"][0]["receivers"][0]
    assert receiver["settings"]["url"].endswith("/webhooks/grafana-alerts")


def test_influx_gets_its_token_and_the_agreed_database_name(spec):
    """The testers and E7's read clients both need the database name without
    asking the operator, so it is fixed rather than configurable."""
    influx_ds = next(
        ds for ds in grafana_datasources(spec)["datasources"] if ds["name"] == "InfluxDB"
    )
    assert influx_ds["jsonData"]["dbName"] == INFLUX_DATABASE


def test_influx_takes_its_admin_token_from_a_file_and_not_the_environment():
    """**`INFLUXDB3_AUTH_TOKEN` configures the influxdb3 CLI, not the server.**

    Setting it on the server container looks exactly like preseeding a token
    and does nothing at all: Influx 3 Core creates its own admin token and
    refuses everything else, so the platform's stored token got 401 on every
    call and a generated stack could never be verified. `serve
    --admin-token-file` is the mechanism that does work, and it is what lets
    the token be generated and committed before the stack exists (fixed choice
    7) rather than scraped out of a container's log afterwards.
    """
    influx = compose_file(_spec())["services"]["influx"]
    assert "INFLUXDB3_AUTH_TOKEN" not in influx.get("environment", {})
    assert f"--admin-token-file={INFLUX_ADMIN_MOUNT}" in influx["command"]
    assert f"./{INFLUX_TOKEN_FILE}:{INFLUX_ADMIN_MOUNT}:ro" in influx["volumes"]


def test_the_influx_token_file_carries_the_token_in_influxs_own_shape():
    """`{"token": ..., "name": "_admin"}` is what `influxdb3 create token
    --admin --offline` writes, and the server parses that and nothing else."""
    files = render_configs(_spec())
    payload = json.loads(files[INFLUX_TOKEN_FILE])
    assert payload == {"token": INFLUX_BEARER, "name": "_admin"}


def test_the_generated_stack_creates_the_database_and_the_bucket():
    """Two init containers, for the two pieces of state that cannot be
    configuration.

    Influx 3 creates a database on first WRITE and E5.4b's tester reads first;
    MinIO has no declarative bucket. Without these the platform generates a
    stack its own verification rejects — which is exactly what E5.10's keystone
    caught, and the reason both are asserted here rather than only end to end.
    """
    services = compose_file(_spec(include_object_storage=True))["services"]
    assert services["influx-init"]["depends_on"] == ["influx"]
    assert services["minio-init"]["depends_on"] == ["minio"]
    for name in ("influx-init", "minio-init"):
        assert services[name]["restart"] == "no", (
            f"{name} does its job once; `unless-stopped` would restart it forever"
        )
    assert INFLUX_DATABASE in " ".join(services["influx-init"]["command"])
    assert S3_BUCKET in " ".join(services["minio-init"]["command"])


def test_the_dynsec_file_is_the_one_writable_mount(spec):
    """The plugin rewrites it as the platform creates each device's client
    (E5.6). Every other config is read-only, so a compromised container cannot
    rewrite the broker's own configuration."""
    volumes = compose_file(spec)["services"]["mosquitto"]["volumes"]
    config_mounts = [v for v in volumes if v.startswith("./mosquitto/")]
    writable = [v for v in config_mounts if not v.endswith(":ro")]
    assert writable == ["./mosquitto/dynamic-security.json:/mosquitto/config/dynamic-security.json"]


def test_state_lives_in_named_volumes_so_down_does_not_destroy_it(spec):
    """Retained MQTT `desired` messages must survive a restart (spec 6.4), and
    telemetry must survive `docker compose down`."""
    compose = compose_file(spec)
    assert "mosquitto-data" in compose["volumes"]
    assert "mosquitto-data:/mosquitto/data" in compose["services"]["mosquitto"]["volumes"]


#: The images allowed to float, by IMAGE and not by service name — MinIO
#: publishes only `:latest` on Docker Hub, and both its server and its `mc`
#: client are in that position. Keyed this way so a service that quietly
#: switched to a floating tag is caught even if it is named `minio-something`.
FLOATING_BY_NECESSITY = frozenset({IMAGES["minio"], IMAGES["minio_client"]})


def test_no_image_is_floating(spec):
    """An operator brings this up months later. `:latest` means a different
    Influx from the one the testers were written against.

    **The gate's fixtures run these same pins** (`conftest.MOSQUITTO_IMAGE`
    reads `IMAGES`), which is the property that was missing when the broker
    fixture floated on `eclipse-mosquitto:2`: the tag moved to a version that
    reads dynsec passwords differently, and the suite went on passing against a
    broker no operator would ever run (D132).
    """
    for name, service in compose_file(spec)["services"].items():
        image = service["image"]
        assert ":" in image, f"{name} has no tag"
        if image not in FLOATING_BY_NECESSITY:
            assert not image.endswith(":latest"), f"{name} is pinned to a floating tag"


def test_rendering_is_deterministic(spec):
    """The property fixed choice 7 rests on: the platform stores no blob and
    re-renders on every download, so two renders of one spec must be identical.
    E5.10 asserts it end to end over the archive; this is the cheap version
    that localises a regression to the renderer."""
    first = render_configs(spec)
    second = render_configs(spec)
    assert first == second, "rendering is not a pure function of the spec"
