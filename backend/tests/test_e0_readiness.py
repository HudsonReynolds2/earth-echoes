"""E0-R readiness flight (Gate 13; project-changes #6; addendum PHASE0-5-01).

The E0 exit-exam: verifies E0.1 through E0.11 as a production-poised whole and
proves every seam later epics consume, grouped by consumer. Each test names
the future task that depends on it — this suite is executable handoff
documentation for E1 through E8 sessions, and its locked-surface contracts are
extended CONSCIOUSLY by later epics, never bypassed.
"""

import re
import subprocess
import sys
import uuid

import pytest
import yaml
from conftest import REPO_ROOT, docker_cli, docker_env, ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.audit import record_audit
from app.auth.passwords import hash_password
from app.auth.service import create_session
from app.db import NAMING_CONVENTION, create_session_factory, metadata
from app.main import API_PREFIX, create_app
from app.models import RoleAssignment, User
from app.secrets import SecretStore
from app.settings import Settings

BACKEND = REPO_ROOT / "backend"
DEPLOY = REPO_ROOT / "deploy"

# The exact public surface E0 ships. Later epics EXTEND these sets in this
# file, deliberately, alongside their INTERFACES.md updates.
E0_ROUTES = {
    ("GET", f"{API_PREFIX}/health"),
    ("POST", f"{API_PREFIX}/auth/login"),
    ("POST", f"{API_PREFIX}/auth/logout"),
    ("GET", f"{API_PREFIX}/auth/me"),
    ("POST", f"{API_PREFIX}/auth/totp/enroll"),
    ("POST", f"{API_PREFIX}/auth/totp/confirm"),
    ("GET", f"{API_PREFIX}/audit"),
    ("GET", f"{API_PREFIX}/users"),
    ("POST", f"{API_PREFIX}/users"),
    ("PATCH", f"{API_PREFIX}/users/{{user_id}}"),
    # E1.2 (gate 21): the spec-13 hierarchy surface, extended here
    # deliberately alongside INTERFACES.md "Owned by E1" (D34, D35). Written
    # from the OpenAPI dump, not by hand. Note: no DELETE /organizations
    # (spec 13; D34, project-changes #13).
    ("GET", f"{API_PREFIX}/organizations"),
    ("POST", f"{API_PREFIX}/organizations"),
    ("GET", f"{API_PREFIX}/organizations/{{organization_id}}"),
    ("PATCH", f"{API_PREFIX}/organizations/{{organization_id}}"),
    ("GET", f"{API_PREFIX}/deployments"),
    ("POST", f"{API_PREFIX}/deployments"),
    ("GET", f"{API_PREFIX}/deployments/{{deployment_id}}"),
    ("PATCH", f"{API_PREFIX}/deployments/{{deployment_id}}"),
    ("DELETE", f"{API_PREFIX}/deployments/{{deployment_id}}"),
    ("GET", f"{API_PREFIX}/pods"),
    ("POST", f"{API_PREFIX}/pods"),
    ("GET", f"{API_PREFIX}/pods/{{pod_id}}"),
    ("PATCH", f"{API_PREFIX}/pods/{{pod_id}}"),
    ("DELETE", f"{API_PREFIX}/pods/{{pod_id}}"),
    ("GET", f"{API_PREFIX}/aggregators"),
    ("POST", f"{API_PREFIX}/aggregators"),
    ("GET", f"{API_PREFIX}/aggregators/{{aggregator_id}}"),
    ("PATCH", f"{API_PREFIX}/aggregators/{{aggregator_id}}"),
    ("DELETE", f"{API_PREFIX}/aggregators/{{aggregator_id}}"),
    ("GET", f"{API_PREFIX}/listeners"),
    ("POST", f"{API_PREFIX}/listeners"),
    ("GET", f"{API_PREFIX}/listeners/{{mac}}"),
    ("PATCH", f"{API_PREFIX}/listeners/{{mac}}"),
    ("DELETE", f"{API_PREFIX}/listeners/{{mac}}"),
    # E1.6 (gate 25): bulk import, CSV/JSON with per-row results (D38).
    ("POST", f"{API_PREFIX}/listeners/import"),
    ("POST", f"{API_PREFIX}/aggregators/import"),
    # E1.7 (gate 26): tags on every entity, PUT = wholesale replace.
    ("GET", f"{API_PREFIX}/organizations/{{organization_id}}/tags"),
    ("PUT", f"{API_PREFIX}/organizations/{{organization_id}}/tags"),
    ("GET", f"{API_PREFIX}/deployments/{{deployment_id}}/tags"),
    ("PUT", f"{API_PREFIX}/deployments/{{deployment_id}}/tags"),
    ("GET", f"{API_PREFIX}/pods/{{pod_id}}/tags"),
    ("PUT", f"{API_PREFIX}/pods/{{pod_id}}/tags"),
    ("GET", f"{API_PREFIX}/aggregators/{{aggregator_id}}/tags"),
    ("PUT", f"{API_PREFIX}/aggregators/{{aggregator_id}}/tags"),
    ("GET", f"{API_PREFIX}/listeners/{{mac}}/tags"),
    ("PUT", f"{API_PREFIX}/listeners/{{mac}}/tags"),
    # E2.1 (gate 31): the settings-catalog read - a schema document, not a
    # D7 list (D47); seeded in-migration from app/config/catalog.py, the
    # single source a gate test pins against spec 5.3.
    ("GET", f"{API_PREFIX}/config/catalog"),
    # E2.4 (gate 34): effective + override endpoints on all five entities
    # (spec 13; D50-D51). Reads VIEW_STATUS, writes MANAGE_CONFIG + CSRF;
    # the D35 scope rules carry over; responses are always redacted.
    ("GET", f"{API_PREFIX}/organizations/{{organization_id}}/config/effective"),
    ("GET", f"{API_PREFIX}/organizations/{{organization_id}}/config/overrides"),
    ("PUT", f"{API_PREFIX}/organizations/{{organization_id}}/config/overrides"),
    ("GET", f"{API_PREFIX}/deployments/{{deployment_id}}/config/effective"),
    ("GET", f"{API_PREFIX}/deployments/{{deployment_id}}/config/overrides"),
    ("PUT", f"{API_PREFIX}/deployments/{{deployment_id}}/config/overrides"),
    ("GET", f"{API_PREFIX}/pods/{{pod_id}}/config/effective"),
    ("GET", f"{API_PREFIX}/pods/{{pod_id}}/config/overrides"),
    ("PUT", f"{API_PREFIX}/pods/{{pod_id}}/config/overrides"),
    ("GET", f"{API_PREFIX}/aggregators/{{aggregator_id}}/config/effective"),
    ("GET", f"{API_PREFIX}/aggregators/{{aggregator_id}}/config/overrides"),
    ("PUT", f"{API_PREFIX}/aggregators/{{aggregator_id}}/config/overrides"),
    ("GET", f"{API_PREFIX}/listeners/{{mac}}/config/effective"),
    ("GET", f"{API_PREFIX}/listeners/{{mac}}/config/overrides"),
    ("PUT", f"{API_PREFIX}/listeners/{{mac}}/config/overrides"),
    # E2.5 (gate 35): the selection engine (spec 5.2, 13; D54) - preview/
    # list/create ONLY; saved selections re-evaluate at use through the
    # caller's visible deployments, never a materialized id list.
    ("POST", f"{API_PREFIX}/selections/preview"),
    ("GET", f"{API_PREFIX}/selections"),
    ("POST", f"{API_PREFIX}/selections"),
    # E2.6 (gate 36): bulk preview/apply (ONE body, ONE plan builder - the
    # parity guarantee, D56) and the spec-13 revisions read surface no task
    # claimed, assigned here (D55). Apply stops at draft unconditionally.
    ("POST", f"{API_PREFIX}/config/preview"),
    ("POST", f"{API_PREFIX}/config/apply"),
    ("GET", f"{API_PREFIX}/aggregators/{{aggregator_id}}/revisions"),
    ("GET", f"{API_PREFIX}/listeners/{{mac}}/revisions"),
    ("GET", f"{API_PREFIX}/revisions/{{revision_id}}"),
    # E3.7 (gate 45): the operator publish action (D82, project-changes #22).
    # The worker never republishes - `auto_reconcile` is stored and inert - so
    # this route is how drift is repaired. E3.13 wires E2's bulk apply to the
    # same `publish_revision` beside it.
    ("POST", f"{API_PREFIX}/revisions/{{revision_id}}/publish"),
    # E3.10 (gate 48): the spec 7.2 command channel. 202, not 200 - the
    # platform published to an unretained topic, it did not watch the device
    # restart. Every submission mints a fresh `command_id` (spec 7.4): the
    # device dedupes its own retries, and two operator submissions are two
    # decisions.
    ("POST", f"{API_PREFIX}/aggregators/{{aggregator_id}}/commands"),
    # E3.11 (gate 49): the spec 6.3 per-device timeline. The ORG-wide and
    # per-deployment halves of spec 6.3 are E0.8's `GET /audit` filtered by
    # scope, deliberately not rebuilt here - two answers to one question
    # would drift apart.
    ("GET", f"{API_PREFIX}/aggregators/{{aggregator_id}}/timeline"),
    ("GET", f"{API_PREFIX}/listeners/{{mac}}/timeline"),
    # E3.12 (gate 50): live updates. `WS /ws` is a websocket route and does
    # not appear in the OpenAPI paths this set is built from, which is why
    # it is absent here rather than forgotten - `test_websockets.py` is its
    # contract.
}

E0_TABLES = {
    "user",
    "session",
    "role_assignment",
    "audit_log",
    "secret",
    "alembic_version",
    # E1.1 (gate 20): the hierarchy tables, extended here deliberately
    # alongside the INTERFACES.md "Owned by E1" section (D30-D32).
    "organization",
    "deployment",
    "pod",
    "aggregator",
    "listener",
    # E1.5 (gate 24): report-time identity tables (D37) - quarantine holds
    # conflicting reports instead of inventory mutations; alerts dedupe open
    # rows per (type, entity).
    "quarantined_report",
    "inventory_alert",
    # E2.1 (gate 31): the versioned settings catalog (spec 5.3 as data;
    # D47-D49), upsert-seeded by its migration so replays converge.
    "settings_catalog",
    # E2.2 (gate 32): one sparse override map per hierarchy entity (D50-D51);
    # secret keys store markers, plaintext rides SecretStore.
    "entity_override",
    # E2.5 (gate 35): saved selections - the validated query document
    # verbatim, re-evaluated at every use (D54).
    "selection",
    # E2.6 (gate 36): immutable per-device desired-config snapshots (D55);
    # E2 writes draft ONLY, E3 owns every other state.
    "config_revision",
    # E3.1 (gate 39): broker connection storage, 'mqtt' rows only. E5 EXTENDS
    # this table with the remaining deployment services and their status
    # lifecycle (spec 16); it does not add a second table.
    "deployment_service",
    # E3.5 (gate 44): what a device reports back. `device_state` is spec 6.1's
    # "last state the device sent", one row per device replaced in place, and
    # E3.9 EXTENDS it with the spec 6.5 Listener liveness block, which arrives
    # inside a report. `device_event` is the spec 7.3 event stream as immutable
    # evidence, deduped per (emitter, instant, code) against QoS 1 redelivery.
    "device_state",
    "device_event",
    # E3.8 (gate 46): the spec 9.3 live online verdict, LWT-driven. A table of
    # its own rather than the `device_state` columns E3.5 anticipated: that
    # row is a REPORT (three NOT NULL columns a status message cannot fill),
    # LWT is Aggregator-only, and an `offline` will is published by the broker
    # rather than by the device. See D88.
    "aggregator_status",
    # E3.11 (gate 49): one row per spec 6.2 transition, written by
    # `revision_state.transition` and nothing else - which is what makes a
    # device timeline complete by construction. Append-only evidence, every
    # reference out of it un-FK'd (D33) so history outlives the revision it
    # describes and the device it happened to.
    "reconciliation_event",
}


@pytest.fixture(scope="module")
def pg_url():
    with ephemeral_postgres() as url:
        yield url


@pytest.fixture(scope="module")
def app(pg_url):
    return create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate13-readiness",
            kek=make_kek(),
            cors_origins="",
        )
    )


# =========================================================================
# A. Locked surface contracts
# =========================================================================


def _openapi(app) -> dict:
    client = TestClient(app, raise_server_exceptions=False)
    return client.get(f"{API_PREFIX}/openapi.json").json()


@pytest.mark.integration
def test_route_surface_is_exactly_the_e0_contract(app):
    schema = _openapi(app)
    actual = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    }
    assert actual == E0_ROUTES, (
        f"public surface drifted; added={actual - E0_ROUTES} removed={E0_ROUTES - actual}. "
        "Later epics extend E0_ROUTES here deliberately, with INTERFACES.md."
    )


@pytest.mark.integration
def test_table_set_is_exactly_the_e0_contract(pg_url):
    engine = create_engine(pg_url)
    try:
        actual = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert actual == E0_TABLES, (
        f"schema drifted; added={actual - E0_TABLES} removed={E0_TABLES - actual}. "
        "A neighboring phase's table appearing early is scope creep (rule R2)."
    )


def test_env_example_and_settings_agree():
    documented = {
        line.split("=")[0]
        for line in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    settings_aliases = {
        field.validation_alias for field in Settings.model_fields.values() if field.validation_alias
    }
    compose_text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    postgres_bootstrap = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
    # Every documented name is really consumed somewhere.
    for name in documented:
        consumed = (
            name in settings_aliases or name in postgres_bootstrap or f"${{{name}" in compose_text
        )
        assert consumed, f".env.example documents {name}, which nothing consumes"
    # Every Settings alias is documented (EOE_BUILD_SHA and EOE_CONFIG_FILE are
    # injection/runtime mechanisms, not deployment configuration).
    for alias in settings_aliases - {"EOE_BUILD_SHA"}:
        assert alias in documented, f"Settings consumes {alias}, undocumented in .env.example"


@pytest.mark.integration
def test_every_operation_declares_responses(app):
    schema = _openapi(app)
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            assert operation.get("responses"), f"no responses declared: {method} {path}"


# =========================================================================
# B. Seams for E1 (hierarchy and inventory)
# =========================================================================


@pytest.mark.integration
def test_role_assignment_deployment_id_now_fks_the_deployment_table(pg_url):
    """E1.1 closed the seam this test's predecessor held open (phase-0 E0.7;
    DECISIONS D33): deployment_id carries a real FK, and it stays nullable
    because NULL still means the grant is organization-wide."""
    engine = create_engine(pg_url)
    try:
        inspector = inspect(engine)
        column = next(
            c for c in inspector.get_columns("role_assignment") if c["name"] == "deployment_id"
        )
        assert column["nullable"] is True, "NULL scope = org-wide grant; must stay nullable"
        assert "UUID" in str(column["type"]).upper()
        fk_targets = {
            fk["referred_table"]
            for fk in inspector.get_foreign_keys("role_assignment")
            if "deployment_id" in fk["constrained_columns"]
        }
        assert fk_targets == {"deployment"}, (
            f"role_assignment.deployment_id should FK the deployment table, got {fk_targets}"
        )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_audit_scope_is_deliberately_never_a_foreign_key(pg_url):
    """PERMANENT, not a seam (DECISIONS D33, D3): audit rows are immutable and
    outlive the deployments they reference, so audit_log.scope must never gain
    a foreign key. A later epic 'fixing' this breaks audit retention."""
    engine = create_engine(pg_url)
    try:
        inspector = inspect(engine)
        column = next(c for c in inspector.get_columns("audit_log") if c["name"] == "scope")
        assert column["nullable"] is True, "NULL scope = organization-wide"
        assert "UUID" in str(column["type"]).upper()
        fk_columns = {
            name
            for fk in inspector.get_foreign_keys("audit_log")
            for name in fk["constrained_columns"]
        }
        assert "scope" not in fk_columns, (
            "audit_log.scope must never be a foreign key (D3 immutability; D33)"
        )
    finally:
        engine.dispose()


def test_audit_entity_id_fits_a_mac_address():
    """E1 keys Listeners by MAC (spec 4.2); audit rows must hold one."""
    entity_id = next(c for c in metadata.tables["audit_log"].columns if c.name == "entity_id")
    assert entity_id.type.length is not None and entity_id.type.length >= 17


def test_naming_convention_intact_for_e1_autogenerate():
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}
    assert metadata.naming_convention == NAMING_CONVENTION


# =========================================================================
# C. Seams for E3 / E4 / E5 (SecretStore consumers; redis readiness)
# =========================================================================


@pytest.mark.integration
def test_secret_store_round_trips_every_consumer_name_shape(pg_url):
    """E3.2 (broker credentials), E5.1 (service credentials), E4.5 (bundle
    secrets pre-firmware-encryption) all store under these name shapes
    (INTERFACES.md, SecretStore section)."""
    _, factory = create_session_factory(pg_url)
    store = SecretStore(factory, make_kek())
    deployment = uuid.uuid4()
    bundle = uuid.uuid4()
    shapes = {
        f"deployment:{deployment}:mqtt_password": f"v-{uuid.uuid4().hex}",
        f"deployment:{deployment}:s3_secret_key": f"v-{uuid.uuid4().hex}",
        f"deployment:{deployment}:influx_token": f"v-{uuid.uuid4().hex}",
        f"bundle:{bundle}:wifi_psk": f"v-{uuid.uuid4().hex}",
        f"bundle:{bundle}:stream_key": f"v-{uuid.uuid4().hex}",
        # E2.2 (gate 32, D51): config override secrets - flagged additive
        # extension of this E0-owned contract (INTERFACES, SecretStore
        # section). Note the MAC-keyed listener shape carries colons.
        f"config:pod:{uuid.uuid4()}:network.wifi_password": f"v-{uuid.uuid4().hex}",
        "config:listener:02:EE:0E:01:01:01:network.stream_key": f"v-{uuid.uuid4().hex}",
    }
    for name, value in shapes.items():
        store.put(name, value)
    for name, value in shapes.items():
        assert store.get(name) == value


def test_redis_stays_optional_until_e3():
    settings = Settings(
        database_url="postgresql+psycopg://x:x@localhost:9/x",
        session_secret="s",
        kek=make_kek(),
    )
    assert settings.redis_url is None  # absent-safe; E3.12/E7.3 turn it on


# =========================================================================
# D. Seam for E8.5 (OIDC pluggability, spec 12.2)
# =========================================================================


@pytest.mark.integration
def test_sessions_mint_independently_of_password_auth(app):
    """An OIDC provider (E8.5) authenticates externally and then mints a
    platform session; nothing in the session layer may depend on the
    password path. Executable proof of 'the auth interface is pluggable'."""
    factory = app.state.session_factory
    with factory() as db:
        user = User(
            email=f"oidc-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="!external-identity-no-password",  # never verifiable
        )
        user.role_assignments.append(RoleAssignment(role="viewer", deployment_id=None))
        db.add(user)
        db.flush()
        session = create_session(db, user, ttl_seconds=300)
        db.commit()
        session_id = session.id

    from app.auth.cookies import SESSION_COOKIE, sign_session_id

    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(
        SESSION_COOKIE, sign_session_id(session_id, app.state.settings.session_secret)
    )
    me = client.get(f"{API_PREFIX}/auth/me")
    assert me.status_code == 200
    assert me.json()["assignments"] == [{"role": "viewer", "deployment_id": None}]


# =========================================================================
# E. Production posture
# =========================================================================


@pytest.mark.integration
def test_migrations_reverse_with_real_data_present():
    """The chain round-trips on EMPTY tables at Gate 2; production rollbacks
    happen with rows. Seed every table, then downgrade to base and back."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        with factory() as db:
            user = User(email="rollback@example.com", password_hash=hash_password("x" * 12))
            user.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
            db.add(user)
            db.flush()
            create_session(db, user, ttl_seconds=60)
            record_audit(
                db,
                action="probe.rollback",
                entity_type="probe",
                entity_id="x",
                actor_user_id=user.id,
            )
            db.commit()
        SecretStore(factory, make_kek()).put("probe:rollback", "v")

        env = {**docker_env(), "DATABASE_URL": url}
        for direction in (("downgrade", "base"), ("upgrade", "head")):
            result = subprocess.run(
                [sys.executable, "-m", "alembic", *direction],
                cwd=BACKEND,
                capture_output=True,
                text=True,
                env=env,
                timeout=180,
            )
            assert result.returncode == 0, f"{direction} with data failed:\n{result.stderr}"


@pytest.mark.integration
@pytest.mark.timeout(1200)
def test_prod_frontend_image_actually_serves_the_app():
    """The nginx prod target has only ever been BUILT; production poise means
    it serves the shell (D2: CDN-shaped static delivery for E8)."""
    env = docker_env()
    build = subprocess.run(
        [docker_cli(), "build", "-q", "--target", "prod", str(REPO_ROOT / "frontend")],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    assert build.returncode == 0, build.stderr
    image = build.stdout.strip()
    name = f"eoe-prod-serve-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [docker_cli(), "run", "-d", "--name", name, "-p", "127.0.0.1:0:80", image],
        capture_output=True,
        text=True,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    try:
        ports = subprocess.run(
            [docker_cli(), "port", name, "80/tcp"], capture_output=True, text=True, env=env
        )
        host_port = ports.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        import time
        import urllib.request

        html = ""
        for _ in range(20):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{host_port}/", timeout=3) as resp:
                    html = resp.read().decode("utf-8")
                break
            except Exception:
                time.sleep(0.5)
        assert 'id="root"' in html, "prod image did not serve the app shell"
        assert re.search(r"/assets/[^\"]+\.js", html), "built JS bundle not referenced"
    finally:
        subprocess.run([docker_cli(), "rm", "-f", name], capture_output=True, env=env)


@pytest.mark.integration
@pytest.mark.timeout(1200)
def test_api_image_runs_as_non_root():
    """Production posture (D19): UID 10001, never root."""
    env = docker_env()
    build = subprocess.run(
        [docker_cli(), "build", "-q", str(REPO_ROOT / "backend")],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run(
        [docker_cli(), "run", "--rm", build.stdout.strip(), "id", "-u"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10001", f"api container uid: {result.stdout.strip()}"


def test_compose_declares_healthchecks_and_frontend_api_url():
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    for service in ("api", "frontend", "postgres", "redis"):
        definition = compose["services"][service]
        has_healthcheck = "healthcheck" in definition or service in ("api", "frontend")
        # api and frontend healthchecks live in their Dockerfiles.
        assert has_healthcheck, f"{service} has no health signal"
    frontend_env = compose["services"]["frontend"].get("environment", {})
    assert "VITE_API_BASE_URL" in frontend_env, (
        "compose frontend must receive the browser-perspective API URL (D19)"
    )
    for dockerfile in ("backend/Dockerfile", "frontend/Dockerfile"):
        assert "HEALTHCHECK" in (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
