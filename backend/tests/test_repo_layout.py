"""Gate 1: repository and container scaffolding checks (task E0.1).

Structural half of the E0.1 gate: fixed monorepo layout, env-var template
hygiene, compose topology and ports, README step budget, and proof that the
R0 gate guard machinery actually fails on a skipped test.
"""

import re
import subprocess
import uuid

import pytest
import yaml
from conftest import REPO_ROOT, docker_cli, docker_env, make_kek, run_git
from gate_runner import GateGuard, enforce

__all__ = ["compose_env", "docker_cli", "docker_env"]  # re-exported for peer suites

DEPLOY = REPO_ROOT / "deploy"
CORE_ENV_VARS = ("DATABASE_URL", "EOE_SESSION_SECRET", "EOE_KEK", "REDIS_URL")
# HOST:CONTAINER. The host side moved into the 1xxxx range (PHASE0-2-02,
# project-changes #21) so the stack coexists with other local services; the
# container side is still each service's standard port, which is why no image
# or in-network URL changed. E3.1 added mosquitto: TLS only (spec 7.1), 8883
# being the registered MQTT-over-TLS port, with no 1883 listener anywhere.
FIXED_PORTS = {
    "api": "18000:8000",
    "frontend": "15173:5173",
    "postgres": "15432:5432",
    "redis": "16379:6379",
    "mosquitto": "18883:8883",
}
# Extended by each epic that adds a service, deliberately and with its
# INTERFACES.md entry — the same discipline as E0_ROUTES/E0_TABLES. E3.7
# added `worker`: the reconciliation loop as its own process (spec 3.1, D59).
# It publishes no ports, which is why FIXED_PORTS does not name it.
COMPOSE_SERVICES = {"api", "frontend", "postgres", "redis", "mosquitto", "worker"}

SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY)\w*\s*[=:]\s*[\"']?[A-Za-z0-9+/_\-]{20,}"),
)


def compose_env() -> dict[str, str]:
    """Throwaway per-run values for compose interpolation; never committed.

    **Every variable the compose file interpolates must be pinned here.** Docker
    Compose also reads `deploy/.env`, which sits beside the compose file and is
    a developer's own scratch file — the QA walkthrough tells you to put
    `EOE_PUBLISH_ENABLED` and the sweep cadences in it. Anything left unpinned
    is therefore read from whatever that file happens to say, so the gate
    tests a different stack on a machine that has run the walkthrough than it
    does in CI, where no `.env` exists. That is how E3.7's local gate went red
    on a defect CI could not have seen (D87).
    """
    password = uuid.uuid4().hex
    return {
        **docker_env(),
        "POSTGRES_USER": "eoe",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": "eoe",
        "DATABASE_URL": f"postgresql+psycopg://eoe:{password}@postgres:5432/eoe",
        "EOE_SESSION_SECRET": uuid.uuid4().hex,
        "EOE_KEK": make_kek(),  # must be base64 of 32 bytes (E0.11 validates at startup)
        "REDIS_URL": "redis://redis:6379/0",
        # E3.7's knobs, pinned to the documented defaults. E3.13 flips
        # EOE_PUBLISH_ENABLED on; this line is where that change lands for the
        # container tests, deliberately and not by whatever `.env` says.
        "EOE_PUBLISH_ENABLED": "true",  # E3.13 flipped the default (D61)
        "EOE_TIMEOUT_SWEEP_SECONDS": "30",
        "EOE_DRIFT_SWEEP_SECONDS": "300",
        # The rest of the compose file's interpolations, each pinned to the
        # default the compose file itself declares. `EOE_CORS_ORIGINS` is the
        # one that bites: a walkthrough `.env` carrying the pre-PHASE0-2-02
        # port would have the gate probing a stack whose CORS names an origin
        # the frontend no longer serves from.
        "BUILD_SHA": "dev",
        "EOE_CORS_ORIGINS": "http://localhost:15173",
        "EOE_FRONTEND_API_URL": "http://localhost:18000",
    }


# --- Gate 1 check 1: fixed monorepo layout ---


def test_fixed_repository_layout():
    for relative in (
        "backend/app",
        "backend/tests",
        "frontend/src",
        "deploy",
        "sim",
        "docs",
        "guide",  # client-facing group (project-changes #7, PHASE0-2-01)
    ):
        assert (REPO_ROOT / relative).is_dir(), f"missing fixed directory: {relative}"
    assert (REPO_ROOT / "backend" / "Dockerfile").is_file()
    assert (REPO_ROOT / "frontend" / "Dockerfile").is_file()
    assert (DEPLOY / "docker-compose.yml").is_file()


# --- Gate 1 check 2: .env.example documents names only ---


def test_env_example_documents_all_names_and_no_values():
    text = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    content = [line for line in lines if line and not line.startswith("#")]
    assert content, ".env.example has no variable lines"
    for line in content:
        assert re.fullmatch(r"[A-Z_]+=", line), f"value or malformed line committed: {line!r}"
    names = {line[:-1] for line in content}
    for var in CORE_ENV_VARS:
        assert var in names, f".env.example missing {var}"


# --- Gate 1 check 3: .env ignored; no committed secrets ---


def test_env_file_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "deploy/.env"], cwd=REPO_ROOT, capture_output=True
    )
    assert result.returncode == 0, "deploy/.env is not gitignored"


def test_no_committed_secret_patterns():
    # --others --exclude-standard includes untracked files, closing the gap
    # where a new file escaped the scan until after its introducing gate's
    # close-out commit (DECISIONS D18).
    tracked = run_git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    for name in tracked:
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            assert match is None, f"possible committed secret in {name}: {match.group(0)[:40]!r}"


# --- Gate 1 checks 4 and 5: compose topology and fixed ports ---


def test_compose_defines_exactly_the_expected_services():
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == COMPOSE_SERVICES


def test_compose_env_pins_every_variable_the_compose_file_interpolates():
    """The container tests must not read a developer's `deploy/.env` (D87).

    Compose resolves `${VAR}` from the process environment first and that file
    second, so an unpinned variable makes the gate test whatever the last QA
    walkthrough left behind — which is exactly how a real defect stayed
    invisible to CI while turning the local gate red. Adding an interpolated
    variable to the compose file means adding it to `compose_env`.
    """
    text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    interpolated = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)[:?}-]", text))
    unpinned = interpolated - set(compose_env())
    assert not unpinned, f"compose interpolates these but compose_env does not pin them: {unpinned}"


def test_no_compose_service_disables_its_healthcheck():
    """`compose up --wait` is what the container tests gate on, and on the CI
    runner's Compose a service with `healthcheck: disable: true` ERRORS with
    "has no healthcheck configured" — while the local Compose tolerated it.
    That version split made the gate green here and red in CI (D92).

    Only `disable: true` is banned, because that is exactly what CI rejects and
    the only part decidable from this file. A service with no `healthcheck` key
    inherits its image's HEALTHCHECK (`api` and `frontend` both do, and both
    report healthy); whether an inherited one probes the right thing is a
    question about the Dockerfile, which is why `worker` — sharing the API
    image but serving no HTTP port — has to override rather than inherit.
    """
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    disabled = [
        name
        for name, service in compose["services"].items()
        if (service.get("healthcheck") or {}).get("disable")
    ]
    assert not disabled, f"`compose up --wait` fails in CI for these: {disabled}"


def test_compose_publishes_the_fixed_ports():
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    for service, mapping in FIXED_PORTS.items():
        ports = compose["services"][service].get("ports", [])
        assert mapping in ports, f"{service} does not publish {mapping}: {ports}"


# --- Gate 1 check 6: README dev setup in at most ten steps ---


def test_readme_dev_setup_within_ten_steps():
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"## Development setup\n(.*?)(?:\n## |\Z)", text, re.S)
    assert match, "README has no '## Development setup' section"
    steps = re.findall(r"^\d+\. ", match.group(1), re.M)
    assert 1 <= len(steps) <= 10, f"dev setup must be 1..10 numbered steps, found {len(steps)}"


# --- Gate 1 check 7: compose file is valid per docker itself ---


def test_docker_compose_config_validates():
    result = subprocess.run(
        [docker_cli(), "compose", "-f", str(DEPLOY / "docker-compose.yml"), "config", "-q"],
        capture_output=True,
        text=True,
        env=compose_env(),
    )
    assert result.returncode == 0, f"docker compose config failed: {result.stderr}"


# --- Gate 1 check 8: the R0 guard demonstrably fails on a skipped test ---


def test_gate_guard_counts_skips_in_a_scratch_suite(tmp_path):
    (tmp_path / "test_scratch.py").write_text(
        "import pytest\n\n\n@pytest.mark.skip(reason='guard proof')\n"
        "def test_scratch():\n    pass\n",
        encoding="utf-8",
    )
    guard = GateGuard()
    code = pytest.main(
        [
            str(tmp_path),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "--override-ini",
            "addopts=",
        ],
        plugins=[guard],
    )
    assert guard.counts.get("skipped") == 1, f"guard saw {guard.counts}"
    assert enforce(guard.counts, int(code)) == 1


def test_enforce_fails_closed_and_passes_clean_runs_through():
    assert enforce({}, 0) == 2
    assert enforce({"skipped": 0, "xfailed": 0, "deselected": 0}, 0) == 0
    assert enforce({"skipped": 0, "xfailed": 1, "deselected": 0}, 0) == 1
    assert enforce({"skipped": 0, "xfailed": 0, "deselected": 3}, 0) == 1
    assert enforce({"skipped": 0, "xfailed": 0, "deselected": 0}, 5) == 5


# --- E5.12b (D158): runtime data files must be inside the image -------------


def test_runtime_data_files_are_inside_the_image():
    """Every non-Python file `app/` reads at runtime must ship in the API image.

    **This test exists because a green gate shipped a 500.** `stack.readme()`
    read `deploy/stack-templates/README.md`, which is exactly where the phase
    document said to put it — and the API image's Docker build context is
    `backend/` (see `deploy/docker-compose.yml`), so nothing outside `backend/`
    is in the image at all. Every test passed, because tests run from the repo
    working tree where both paths exist; the download endpoint then raised
    `FileNotFoundError` on a real container the first time a human clicked it.

    The rule this pins: a path `app/` opens at runtime resolves INSIDE
    `backend/`. `COPY app ./app` then ships it by construction rather than by
    someone remembering to add a second COPY.
    """
    from app.services import stack

    context = REPO_ROOT / "backend"
    runtime_dirs = {
        # name -> the directory the module resolves at import time
        "stack.TEMPLATE_DIR": stack.TEMPLATE_DIR,
    }
    for name, path in runtime_dirs.items():
        resolved = path.resolve()
        assert resolved.is_dir(), f"{name} does not exist: {resolved}"
        assert resolved.is_relative_to(context.resolve()), (
            f"{name} resolves to {resolved}, which is OUTSIDE the API image's build "
            f"context ({context}). It will not exist in the container and every "
            f"request that reads it will 500. Move it under backend/app/."
        )
        assert any(resolved.iterdir()), f"{name} is an empty directory: {resolved}"


def test_the_dockerfile_copies_everything_app_reads():
    """The other half: `COPY app ./app` is what makes the test above sufficient.

    If the Dockerfile ever stops copying the whole `app` tree — copying only
    `*.py`, say — then living inside `backend/app` stops being enough and this
    fails rather than the operator's download.
    """
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY app ./app" in dockerfile, (
        "the API image no longer copies the whole app tree; non-Python runtime "
        "data files (app/services/stack_templates/) may not be in the image"
    )
