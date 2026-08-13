"""E5.10, the keystone: the generated bundle is brought up and verified.

**This is spec 16.3's own sentence executed rather than described.** Every other
test in this epic checks a rendered string, a stored row or a mocked dial. This
one takes the archive the platform would hand an operator, unpacks it, runs
`docker compose up -d`, and points all five E5.4 testers at the result — the
same testers, unmodified, that an operator triggers from the services screen.
If they pass and `services_status` reaches `verified`, then the thing the gate
proves is the thing that ships.

Why it is worth its runtime: every other assertion in E5.8/E5.9/E5.10 is about
a file the platform WROTE. Nothing else checks that a Mosquitto actually starts
from that conf, that Influx accepts that token, or that Prometheus's basic auth
matches the hash beside it. Those are exactly the failures an operator would
hit first and the platform would report as their fault.

Ports are remapped to ephemeral ones by an override file. The generated compose
publishes fixed ports because that is what an operator wants; a gate that bound
8883 and 3000 on the shared host would collide with the dev stack and with
itself under xdist. Only the port mappings are overridden — images, configs and
every credential are the generated ones.

**It has paid for itself five times, and each one is a defect no unit test in
this epic could have had.** In the order they were found:

1. The bundle shipped `server.key` at 0600, and Mosquitto — which drops to uid
   1883 — would not start: `Unable to load server key file`.
2. `prometheus.yml` referenced a `scrape_password` file `render_configs` never
   produced, so Docker created a directory in its place and Prometheus scraped
   nothing.
3. The broker's `dynamic-security.json` wrote `encoded_password`, which
   **Mosquitto 2.0 ignores**, leaving the platform account with no password at
   all: CONNACK 135 on every connect. Every dynsec test in the suite passed
   because the fixtures floated on `eclipse-mosquitto:2` and Docker Hub had
   moved that tag to 2.1.x (D132).
4. Influx got its token through `INFLUXDB3_AUTH_TOKEN`, which configures the
   CLI and not the server, so the platform's own token was refused; and the
   `echoes` database did not exist, because Influx 3 creates one on first write
   and the tester reads first.
5. Object storage had no bucket, and Grafana had no service account — the
   platform was sending an admin password as a bearer token.

Every one of those is a file the platform WROTE being wrong about the software
that reads it, which is exactly the class of failure an operator meets first and
the platform reports as their fault.
"""

import json
import socket
import subprocess
import time
import uuid

import pytest
import yaml
from conftest import docker_cli, docker_env, ephemeral_postgres, make_kek

from app.db import create_session_factory
from app.models import Deployment, Organization
from app.secrets import SecretStore
from app.services import bundle, stackgen, store
from app.services.provision import ensure_service_credentials
from app.services.stack import PORTS
from app.services.status import apply_test_results, recompute
from app.services.testers import REGISTRY, resolve_credentials, run_testers

pytestmark = pytest.mark.anyio

SLUG = "keystone"
#: Grafana is the slow one; the whole bring-up is well under this, and the cap
#: exists so a wedged container names itself instead of holding the gate.
READY_TIMEOUT = 180


def _compose(project: str, workdir, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [docker_cli(), "compose", "-p", project, *args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        env=docker_env(),
    )


def _published(project: str, workdir, service: str, container_port: int) -> int:
    result = _compose(project, workdir, "port", service, str(container_port))
    assert result.returncode == 0, f"no published port for {service}: {result.stderr}"
    return int(result.stdout.strip().splitlines()[0].rsplit(":", 1)[1])


def _wait_for_init(project: str, workdir, service: str) -> None:
    """Wait for a one-shot init container to finish, and fail if it failed.

    `docker compose up -d` returns as soon as containers are STARTED, so
    without this the testers race the container that creates the Influx
    database and the one that creates the bucket — and the resulting failure
    would read as "the stack is broken" rather than "the stack was not ready".
    A non-zero exit is a real failure and is reported with the container's own
    log, because that is where the reason is.
    """
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        result = _compose(
            project, workdir, "ps", "-a", "--format", "{{.Service}} {{.State}} {{.ExitCode}}"
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == service and parts[1] == "exited":
                assert parts[2] == "0", (
                    f"{service} exited {parts[2]}:\n"
                    + _compose(project, workdir, "logs", service).stdout
                )
                return
        time.sleep(1)
    raise AssertionError(f"{service} never finished in {READY_TIMEOUT}s")


def _wait_for_port(label: str, port: int) -> None:
    """Readiness by accepting a connection, not by log-scraping.

    The first version of this waited for each service's startup banner, and
    Mosquitto does not print one under the generated conf's `log_type` set — so
    a broker that was serving perfectly well looked dead for 180 seconds. The
    port answering is the property the testers actually need, and it is the
    same thing `conftest._wait_until_accepting` waits on for the dev broker.
    """
    deadline = time.time() + READY_TIMEOUT
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return
        except OSError as error:
            last = error
            time.sleep(1)
    raise AssertionError(f"{label} never accepted on 127.0.0.1:{port} in {READY_TIMEOUT}s: {last}")


def _wait_for_http(label: str, url: str, ok: tuple[int, ...] = (200, 401, 403)) -> None:
    """An open port is not a served application — Grafana accepts long before
    it answers. Any of these codes means the HTTP stack is up; 401/403 count
    because these services are credentialed and refusing is still answering."""
    import httpx

    deadline = time.time() + READY_TIMEOUT
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=5.0)
            if response.status_code in ok:
                return
            last = f"HTTP {response.status_code}"
        except Exception as error:  # noqa: BLE001 - any dial failure is "not yet"
            last = repr(error)
        time.sleep(1)
    raise AssertionError(f"{label} never answered at {url} in {READY_TIMEOUT}s: {last}")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.timeout(600)
async def test_the_generated_bundle_comes_up_and_every_tester_passes(tmp_path):
    """**The E5.10 keystone.**

    Generate → download → unpack → `docker compose up -d` → run all five
    testers → `services_status` is `verified`.
    """
    project = f"eoe-keystone-{uuid.uuid4().hex[:8]}"
    work = tmp_path / "bundle"

    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        secret_store = SecretStore(factory, make_kek())
        with factory() as db:
            org = Organization(name="keystone-org")
            db.add(org)
            db.flush()
            deployment = Deployment(
                id=uuid.uuid4(), organization_id=org.id, name="Keystone", slug=SLUG
            )
            db.add(deployment)
            db.commit()
            db.refresh(deployment)

            # 1. Generate, then render exactly what a download would produce.
            generated = stackgen.generate_stack(
                db,
                secret_store,
                deployment,
                include_object_storage=True,
                hostnames=("localhost",),
                ips=("127.0.0.1",),
            )
            tls = stackgen.tls_material(secret_store, deployment.id)
            archive = bundle.build_archive(bundle.bundle_files(generated, tls))

            # 2. Unpack it the way an operator would. `tmp_path`, never the
            #    repo: a rendered bundle carries real-shaped credentials and
            #    `SECRET_PATTERNS` scans the tree.
            work.mkdir()
            bundle.unpack(archive, work)
            root = work / bundle.ROOT
            assert (root / "docker-compose.yml").is_file()

            # 3. Ephemeral host ports, so the gate does not bind 8883 or 3000
            #    on a shared machine.
            #
            #    Rewritten IN PLACE rather than through a
            #    `docker-compose.override.yml`, because Compose merges `ports`
            #    by CONCATENATION, not replacement: an override adds `0:9000`
            #    beside the original `9000:9000` and the fixed bind happens
            #    anyway. That cost a run — the failure was
            #    "Bind for 0.0.0.0:9000 failed: port is already allocated" on a
            #    host with another project's MinIO up.
            #
            #    Only the HOST side of each mapping changes. Images, configs,
            #    volumes and every credential are the generated ones.
            compose = yaml.safe_load((root / "docker-compose.yml").read_text())
            for spec in compose["services"].values():
                if spec.get("ports"):
                    spec["ports"] = [f"0:{str(m).split(':')[1]}" for m in spec["ports"]]
            (root / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))

            try:
                up = _compose(project, root, "up", "-d")
                assert up.returncode == 0, f"the generated stack did not come up:\n{up.stderr}"

                ports = {
                    "mqtt": _published(project, root, "mosquitto", PORTS["mosquitto"]),
                    "influx": _published(project, root, "influx", PORTS["influx"]),
                    "prometheus": _published(project, root, "prometheus", PORTS["prometheus"]),
                    "grafana": _published(project, root, "grafana", PORTS["grafana"]),
                    "s3": _published(project, root, "minio", PORTS["minio"]),
                }

                _wait_for_port("mosquitto", ports["mqtt"])
                _wait_for_http("influx", f"http://127.0.0.1:{ports['influx']}/health")
                _wait_for_http("prometheus", f"http://127.0.0.1:{ports['prometheus']}/-/ready")
                _wait_for_http("grafana", f"http://127.0.0.1:{ports['grafana']}/api/health")
                _wait_for_http("minio", f"http://127.0.0.1:{ports['s3']}/minio/health/live")
                _wait_for_init(project, root, "influx-init")
                _wait_for_init(project, root, "minio-init")

                # 4. Re-point the stored rows at the ports compose actually
                #    published. An operator's rows carry the real ones; only
                #    this test's ephemeral mapping differs, and the testers
                #    must dial what is running.
                _repoint(db, deployment.id, ports)
                db.commit()

                # 5. The credential Grafana will only issue once it is running.
                #    The endpoint calls this before it resolves credentials, so
                #    the keystone does too — a generated stack has a Grafana
                #    ADMIN account and no service account token until this runs.
                await ensure_service_credentials(db, secret_store, deployment.id)
                db.commit()

                # 6. The real testers, unmodified, exactly as the services
                #    screen runs them.
                rows = {r.service_key: r for r in store.load_services(db, deployment.id)}
                credentials = {
                    key: resolve_credentials(
                        key,
                        rows.get(key),
                        None,
                        secret_store.get,
                        deployment_id=deployment.id,
                        deployment_slug=SLUG,
                    )
                    for key in REGISTRY
                }
                results = await run_testers(
                    [REGISTRY[key] for key in REGISTRY], credentials, whole_call_budget=180.0
                )

                failed = [
                    (r.service_key, r.outcome, [c.detail for c in r.checks if not c.passed])
                    for r in results
                    if r.outcome != "pass"
                ]
                assert not failed, (
                    "the platform generated a stack its own testers reject:\n"
                    + json.dumps(failed, indent=2)
                )

                # 7. And the rollup says so.
                apply_test_results(db, deployment.id, results)
                recompute(db, deployment.id)
                db.commit()
                db.refresh(deployment)
                assert deployment.services_status == "verified", (
                    "every tester passed but the deployment did not reach `verified`; "
                    "spec 16.5 gates provisioning on this value"
                )
            finally:
                _compose(project, root, "down", "-v", "--remove-orphans")


def _repoint(db, deployment_id, ports) -> None:
    """Point the stored rows at the ephemeral ports compose published."""
    rows = {r.service_key: r for r in store.load_services(db, deployment_id)}
    rows["mqtt"].port = ports["mqtt"]
    rows["influx"].config = {
        **rows["influx"].config,
        "url": f"http://127.0.0.1:{ports['influx']}",
    }
    rows["prometheus"].config = {
        **rows["prometheus"].config,
        "read_url": f"http://127.0.0.1:{ports['prometheus']}",
        "remote_write_url": f"http://127.0.0.1:{ports['prometheus']}/api/v1/write",
    }
    rows["grafana"].config = {
        **rows["grafana"].config,
        "base_url": f"http://127.0.0.1:{ports['grafana']}",
    }
    rows["s3"].config = {
        **rows["s3"].config,
        "endpoint": f"http://127.0.0.1:{ports['s3']}",
    }
