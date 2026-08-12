"""The warm container pool's own guarantees (task INFRA.1).

The pool replaced one Postgres container per test module with one machine-wide
server handing out clones of a migrated template. That is a large change to the
foundation every other suite stands on, and its failure mode is the dangerous
kind: a database that is subtly not what the caller asked for makes some OTHER
suite red — or, far worse, green — for reasons three layers from the cause.

So the contract is asserted here, directly, rather than inferred from the rest
of the suite passing. `ephemeral_postgres` promises a migrated, empty, private
database; each of those three words gets a test, plus the two properties that
make the pool safe to share between concurrent agents.
"""

import subprocess
import sys

import pytest
from conftest import (
    REPO_ROOT,
    _acquire_pool_postgres,
    _schema_fingerprint,
    docker_env,
    ephemeral_postgres,
)
from sqlalchemy import create_engine, inspect, text


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _row_counts(url: str) -> dict[str, object]:
    """Every table's row count — except `alembic_version`, which contributes the
    stamped revision itself, so comparing two of these dicts also compares the
    two revisions the databases actually reached rather than just agreeing that
    each has one row."""
    engine = create_engine(url)
    try:
        counts: dict[str, object] = {}
        with engine.connect() as connection:
            for table in sorted(inspect(engine).get_table_names()):
                query = (
                    "SELECT version_num FROM alembic_version"
                    if table == "alembic_version"
                    else f'SELECT count(*) FROM "{table}"'
                )
                counts[table] = connection.execute(text(query)).scalar()
        return counts
    finally:
        engine.dispose()


def test_a_pooled_database_has_exactly_the_schema_a_real_migration_run_produces():
    """The template is only trustworthy if cloning it is indistinguishable from
    migrating from scratch, so both are done here and compared.

    `migrate=False` clones `template0` and then runs alembic for real, which is
    the same path `test_migrations` takes; `migrate=True` clones the template
    the pool built. Comparing the two catches the failure that would otherwise
    be invisible — a template built at some earlier revision and reused after
    the migration set moved.
    """
    with ephemeral_postgres() as cloned, ephemeral_postgres(migrate=False) as fresh:
        upgraded = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT / "backend",
            capture_output=True,
            text=True,
            env={**docker_env(), "DATABASE_URL": fresh},
            timeout=180,
        )
        assert upgraded.returncode == 0, upgraded.stderr

        assert _tables(cloned) == _tables(fresh)
        assert _row_counts(cloned) == _row_counts(fresh)


#: Tables a migration deliberately POPULATES, so "a clean database" does not
#: mean "no rows anywhere". `settings_catalog` is E2.1's catalog, seeded by the
#: migration that creates it — which is why the schema test above compares row
#: counts against a real migration run rather than against zero.
SEEDED_BY_MIGRATIONS = frozenset({"alembic_version", "settings_catalog"})


def test_a_pooled_database_carries_no_application_rows():
    """Nothing but what a migration put there.

    The template is written to exactly once, by alembic, and then only ever
    cloned — but "only ever" is the kind of claim that stops being true the
    first time somebody seeds a fixture against the wrong URL, and every suite
    in the repository assumes a clean database at setup. This is the cheap
    direct check; the row-count comparison above is the exhaustive one.
    """
    with ephemeral_postgres() as url:
        counts = _row_counts(url)
    assert counts, "a migrated database should have tables"
    populated = {
        table: count
        for table, count in counts.items()
        if count and table not in SEEDED_BY_MIGRATIONS
    }
    assert populated == {}, f"a fresh pooled database arrived carrying rows: {populated}"


def test_two_pooled_databases_are_isolated_from_each_other():
    """Concurrent modules share a server; they must not share a schema.

    Two live at once here because that is the arrangement the gate actually
    runs — six xdist workers, each holding a module's database off one server —
    and a table created in one being visible in the other would mean every
    module in the suite was sharing state without knowing it.
    """
    with ephemeral_postgres() as first, ephemeral_postgres() as second:
        assert first != second

        engine = create_engine(first)
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE pool_isolation_probe (id int)"))
        finally:
            engine.dispose()

        assert "pool_isolation_probe" in _tables(first)
        assert "pool_isolation_probe" not in _tables(second)


def test_a_pooled_database_is_gone_once_it_is_released():
    """The clone is dropped on exit, so an interrupted-run sweep is a backstop
    and not the mechanism. Without this a long-lived pooled server accumulates
    a database per module per run until it runs out of memory."""
    with ephemeral_postgres() as url:
        name = url.rsplit("/", 1)[1]
    port = _acquire_pool_postgres()
    admin = create_engine(
        f"postgresql+psycopg://postgres:eoe-testpool@127.0.0.1:{port}/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as connection:
            found = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
    finally:
        admin.dispose()
    assert found is None, f"{name} outlived its context manager"


def test_the_template_is_keyed_by_the_migration_set_and_not_by_its_head():
    """Two worktrees at the same head with different migration bodies must not
    share a template.

    The fingerprint is what stops one branch inheriting the other's schema, and
    the property it needs is that it moves when any migration file's CONTENT
    moves — not only when a file is added. Asserted by editing a byte and
    putting it back, because the id-based alternative would be stable across
    exactly this change and would hand out the wrong schema.
    """
    versions = sorted((REPO_ROOT / "backend" / "alembic" / "versions").glob("*.py"))
    assert versions, "no migrations to fingerprint"

    before = _schema_fingerprint()
    assert before == _schema_fingerprint(), "the fingerprint is not stable"

    victim = versions[0]
    original = victim.read_bytes()
    try:
        victim.write_bytes(original + b"\n# fingerprint probe\n")
        assert _schema_fingerprint() != before
    finally:
        victim.write_bytes(original)
    assert _schema_fingerprint() == before


@pytest.mark.parametrize("migrate", [True, False])
def test_both_modes_hand_out_a_usable_database(migrate: bool):
    """`migrate=False` is the path `test_migrations` and the E0 readiness
    reverse-migration test take, and it is the reason the template can be
    trusted at all — the suite never stops running a from-scratch migration.
    Both modes are exercised here so neither can rot unnoticed."""
    with ephemeral_postgres(migrate=migrate) as url:
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                assert connection.execute(text("SELECT 1")).scalar() == 1
        finally:
            engine.dispose()
        has_tables = bool(inspect(create_engine(url)).get_table_names())
    assert has_tables is migrate
