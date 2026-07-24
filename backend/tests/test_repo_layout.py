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
from conftest import REPO_ROOT, docker_cli, docker_env, run_git
from gate_runner import GateGuard, enforce

__all__ = ["compose_env", "docker_cli", "docker_env"]  # re-exported for peer suites

DEPLOY = REPO_ROOT / "deploy"
CORE_ENV_VARS = ("DATABASE_URL", "EOE_SESSION_SECRET", "EOE_KEK", "REDIS_URL")
FIXED_PORTS = {
    "api": "8000:8000",
    "frontend": "5173:5173",
    "postgres": "5432:5432",
    "redis": "6379:6379",
}

SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY)\w*\s*[=:]\s*[\"']?[A-Za-z0-9+/_\-]{20,}"),
)


def compose_env() -> dict[str, str]:
    """Throwaway per-run values for compose interpolation; never committed."""
    password = uuid.uuid4().hex
    return {
        **docker_env(),
        "POSTGRES_USER": "eoe",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": "eoe",
        "DATABASE_URL": f"postgresql+psycopg://eoe:{password}@postgres:5432/eoe",
        "EOE_SESSION_SECRET": uuid.uuid4().hex,
        "EOE_KEK": uuid.uuid4().hex,
        "REDIS_URL": "redis://redis:6379/0",
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


def test_compose_defines_exactly_the_four_services():
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"api", "frontend", "postgres", "redis"}


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
