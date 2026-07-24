"""Gate 12: dev seed (task E0.12).

Phase-doc acceptance, proven literally: from a fresh (unmigrated) database,
one command yields credentials that log in as the organization-wide owner.
The password appears exactly once on stdout and is stored only as a hash;
re-seeding is refused.
"""

import re
import subprocess
import sys

import pytest
from conftest import REPO_ROOT, ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import create_session_factory
from app.main import API_PREFIX, create_app
from app.models import AuditLog, User
from app.settings import Settings

BACKEND = REPO_ROOT / "backend"


def _run_seed(url: str, session_secret: str, kek: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.seed"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={
            **dict(__import__("os").environ),
            "DATABASE_URL": url,
            "EOE_SESSION_SECRET": session_secret,
            "EOE_KEK": kek,
        },
        timeout=180,
    )


@pytest.mark.integration
def test_fresh_environment_to_logged_in_owner_in_one_command():
    session_secret = f"seed-{make_kek()[:16]}"
    kek = make_kek()
    # migrate=False: the seed command itself must take the database from
    # completely empty to migrated and seeded (the "one command" acceptance).
    with ephemeral_postgres(migrate=False) as url:
        result = _run_seed(url, session_secret, kek)
        assert result.returncode == 0, f"seed failed:\n{result.stdout}\n{result.stderr}"

        email_match = re.search(r"email:\s+(\S+)", result.stdout)
        password_match = re.search(r"password:\s+(\S+)", result.stdout)
        assert email_match and password_match, f"credentials not printed:\n{result.stdout}"
        email, password = email_match.group(1), password_match.group(1)
        assert result.stdout.count(password) == 1, "password must print exactly once"
        assert "never be shown again" in result.stdout

        # The password exists nowhere at rest; only its Argon2id hash does.
        _, factory = create_session_factory(url)
        with factory() as db:
            user = db.scalar(select(User).where(User.email == email))
            assert user is not None
            assert user.password_hash.startswith("$argon2id$")
            assert password not in user.password_hash
            audit = db.scalar(select(AuditLog).where(AuditLog.action == "user.create"))
            assert audit is not None and audit.actor_user_id is None
            assert audit.detail is not None and audit.detail.get("seed") is True
            assert password not in str(audit.detail)

        # The printed credentials log in as an org-wide owner.
        app = create_app(
            Settings(
                database_url=url,
                session_secret=session_secret,
                kek=kek,
                cors_origins="",
            )
        )
        client = TestClient(app, raise_server_exceptions=False)
        login = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        me = client.get(f"{API_PREFIX}/auth/me").json()
        assert me["assignments"] == [{"role": "owner", "deployment_id": None}]

        # Re-seeding is refused with a clear message and a nonzero exit.
        again = _run_seed(url, session_secret, kek)
        assert again.returncode == 1
        assert "already exists" in again.stderr
        assert "password" not in again.stdout.lower()
