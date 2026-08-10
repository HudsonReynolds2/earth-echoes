"""Gate 14: the shippable deployment verifier (E0.12+; project-changes #7).

Runs `python -m app.verify` as an operator would — a subprocess against a
real compose stack — and asserts the full journey passes, the temporary
accounts are gone afterwards, the immutable audit trail survives with nulled
actors, and no password material reaches stdout. The client-facing tool can
never rot: this test executes it at every gate.
"""

import subprocess
import sys

import pytest
from conftest import REPO_ROOT, bootstrap_broker_material
from sqlalchemy import create_engine, text
from test_repo_layout import compose_env, docker_cli

BACKEND = REPO_ROOT / "backend"
DEPLOY = REPO_ROOT / "deploy"
PROJECT = "eoe-verify-test"

pytestmark = pytest.mark.integration


def _compose(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [docker_cli(), "compose", "-f", str(DEPLOY / "docker-compose.yml"), "-p", PROJECT, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )


def test_verifier_walks_the_whole_platform_and_cleans_up():
    env = compose_env()
    # The verifier reaches the stack's Postgres via the published host port,
    # which is not the in-network one (PHASE0-2-02).
    host_db_url = env["DATABASE_URL"].replace("@postgres:5432", "@localhost:15432")
    bootstrap_broker_material()
    try:
        up = _compose("up", "-d", "--build", "--wait", env=env)
        assert up.returncode == 0, f"compose up failed:\n{up.stdout}\n{up.stderr}"

        migrate = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env={**env, "DATABASE_URL": host_db_url},
            timeout=180,
        )
        assert migrate.returncode == 0, migrate.stderr

        result = subprocess.run(
            [sys.executable, "-m", "app.verify", "--api", "http://localhost:18000"],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env={**env, "DATABASE_URL": host_db_url},
            timeout=300,
        )
        assert result.returncode == 0, f"verifier failed:\n{result.stdout}\n{result.stderr}"
        assert "[FAIL]" not in result.stdout
        assert "deployment verified" in result.stdout
        assert result.stdout.count("[PASS]") >= 18, result.stdout

        engine = create_engine(host_db_url)
        try:
            with engine.connect() as connection:
                # Every temporary account is gone...
                leftover = connection.execute(
                    text("SELECT email FROM \"user\" WHERE email LIKE 'verify-%'")
                ).fetchall()
                assert leftover == [], f"temp accounts not cleaned up: {leftover}"
                # ...their sessions and secrets with them...
                secrets_left = connection.execute(
                    text("SELECT count(*) FROM secret WHERE name LIKE 'totp:%'")
                ).scalar()
                assert secrets_left == 0
                # ...while the immutable audit trail survives, actor nulled.
                trail = connection.execute(
                    text(
                        "SELECT count(*) FROM audit_log "
                        "WHERE action = 'user.create' AND actor_user_id IS NULL"
                    )
                ).scalar()
                assert trail and trail >= 1, "verification audit trail missing"
        finally:
            engine.dispose()

        # No password material on stdout: the only credentials ever printed by
        # anything are the seed script's, and the verifier prints none.
        assert "password:" not in result.stdout
    finally:
        down = _compose("down", "-v", "--remove-orphans", env=env)
        assert down.returncode == 0, down.stderr
