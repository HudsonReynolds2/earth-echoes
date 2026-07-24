"""Gate 2: Postgres and migrations checks (task E0.2).

Chain-integrity half runs against the revision graph on disk; behavioural half
runs against a real ephemeral Postgres container (rule R0: Docker is a hard
prerequisite, these never self-skip). The suite protects the append-only,
reversible, one-head migration policy every later phase depends on
(docs/migration-conventions.md).
"""

import ast
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from conftest import REPO_ROOT
from sqlalchemy import create_engine, inspect
from test_repo_layout import docker_cli, docker_env

BACKEND = REPO_ROOT / "backend"
VERSIONS = BACKEND / "alembic" / "versions"
FILENAME_RE = re.compile(r"[0-9a-f]{12}_[a-z0-9_]+\.py")
CONSTRAINT_PREFIX_RE = re.compile(r"(pk|fk|uq|ck|ix)_")
PG_CONTAINER = "eoe-migration-test"
PG_PORT = 54329


def _script_dir() -> ScriptDirectory:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(config)


def _revisions() -> list:
    return list(_script_dir().walk_revisions())


# --- Gate 2 checks 1 and 2: chain shape ---


def test_exactly_one_head_no_branches_no_orphans():
    script = _script_dir()
    assert len(script.get_heads()) == 1, f"multiple heads: {script.get_heads()}"
    revisions = _revisions()
    known = {rev.revision for rev in revisions}
    roots = [rev for rev in revisions if rev.down_revision is None]
    assert len(roots) == 1, f"expected one root revision, found {[r.revision for r in roots]}"
    children: dict[str, int] = {}
    for rev in revisions:
        down = rev.down_revision
        if down is not None:
            assert down in known, f"{rev.revision} revises unknown revision {down}"
            children[down] = children.get(down, 0) + 1
    branch_points = {parent: n for parent, n in children.items() if n > 1}
    assert not branch_points, f"branch points in the chain: {branch_points}"


# --- Gate 2 check 3: every non-root downgrade is real ---


def _downgrade_body(path: Path) -> list[ast.stmt]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            return [
                stmt
                for stmt in node.body
                if not (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                )
            ]
    raise AssertionError(f"{path.name} has no downgrade() function")


def test_every_nonroot_migration_has_a_real_downgrade():
    for rev in _revisions():
        if rev.down_revision is None:
            continue  # the single exempt baseline root (migration-conventions.md)
        body = _downgrade_body(Path(rev.path))
        assert body, f"{rev.revision}: empty downgrade"
        assert not all(isinstance(stmt, ast.Pass) for stmt in body), (
            f"{rev.revision}: downgrade is a bare pass"
        )
        assert not (len(body) == 1 and isinstance(body[0], ast.Raise)), (
            f"{rev.revision}: downgrade only raises"
        )


# --- Gate 2 check 4: revision filenames follow the fixed template ---


def test_revision_filenames_follow_template():
    files = [p for p in VERSIONS.glob("*.py") if p.name != "__init__.py"]
    assert files, "no revision files found"
    for path in files:
        assert FILENAME_RE.fullmatch(path.name), f"bad revision filename: {path.name}"


# --- Gate 2 check 10: the conventions document exists and is complete ---


def test_migration_conventions_documented():
    text = (REPO_ROOT / "docs" / "migration-conventions.md").read_text(encoding="utf-8")
    for phrase in (
        "Append-only history",
        "Every migration reversible",
        "Autogenerate reviewed by hand",
        "Exactly one head",
    ):
        assert phrase in text, f"migration-conventions.md missing: {phrase}"


# --- Gate 2 checks 5 through 9: behaviour against a real Postgres ---


@pytest.fixture(scope="module")
def pg_url():
    password = uuid.uuid4().hex
    env = docker_env()
    subprocess.run([docker_cli(), "rm", "-f", PG_CONTAINER], capture_output=True, env=env)
    run = subprocess.run(
        [
            docker_cli(),
            "run",
            "-d",
            "--name",
            PG_CONTAINER,
            "-p",
            f"{PG_PORT}:5432",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert run.returncode == 0, f"could not start ephemeral postgres: {run.stderr}"
    try:
        ready_streak = 0
        for _ in range(90):
            probe = subprocess.run(
                [docker_cli(), "exec", PG_CONTAINER, "pg_isready", "-U", "postgres"],
                capture_output=True,
                env=env,
            )
            ready_streak = ready_streak + 1 if probe.returncode == 0 else 0
            if ready_streak >= 2:  # survives the init-time restart
                break
            time.sleep(1)
        else:
            raise AssertionError("ephemeral postgres never became ready")
        yield f"postgresql+psycopg://postgres:{password}@localhost:{PG_PORT}/postgres"
    finally:
        subprocess.run([docker_cli(), "rm", "-f", "-v", PG_CONTAINER], capture_output=True, env=env)


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={**docker_env(), "DATABASE_URL": url},
        timeout=120,
    )


@pytest.mark.integration
def test_upgrade_head_from_empty(pg_url):
    result = _alembic(pg_url, "upgrade", "head")
    assert result.returncode == 0, f"upgrade head failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.integration
def test_downgrade_one_from_head(pg_url):
    result = _alembic(pg_url, "downgrade", "-1")
    assert result.returncode == 0, f"downgrade -1 failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.integration
def test_full_round_trip(pg_url):
    for args in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")):
        result = _alembic(pg_url, *args)
        assert result.returncode == 0, f"{args} failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.integration
def test_autogenerate_diff_is_empty_at_head(pg_url):
    from app.db import Base

    _alembic(pg_url, "upgrade", "head")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "compare_server_default": True}
            )
            diff = compare_metadata(context, Base.metadata)
        assert diff == [], f"models and migrations out of sync: {diff}"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_constraints_follow_naming_convention(pg_url):
    _alembic(pg_url, "upgrade", "head")
    engine = create_engine(pg_url)
    try:
        inspector = inspect(engine)
        for table in inspector.get_table_names():
            if table == "alembic_version":
                continue  # alembic-owned bookkeeping table
            names: list[str] = []
            pk = inspector.get_pk_constraint(table)
            if pk and pk.get("name"):
                names.append(pk["name"])
            names += [fk["name"] for fk in inspector.get_foreign_keys(table) if fk.get("name")]
            names += [
                uq["name"] for uq in inspector.get_unique_constraints(table) if uq.get("name")
            ]
            names += [ck["name"] for ck in inspector.get_check_constraints(table) if ck.get("name")]
            names += [ix["name"] for ix in inspector.get_indexes(table) if ix.get("name")]
            for name in names:
                assert CONSTRAINT_PREFIX_RE.match(name), (
                    f"constraint {name!r} on {table!r} violates the naming convention"
                )
    finally:
        engine.dispose()
