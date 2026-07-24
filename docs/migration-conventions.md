# Migration Conventions

Binding for every phase (task E0.2; enforced by `backend/tests/test_migrations.py`, which no
later session may weaken). Alembic lives in `backend/alembic/`; the URL comes exclusively
from `DATABASE_URL`.

## The four conventions

1. **Append-only history.** A migration that has been committed is never edited, reordered,
   or deleted. Fixing a mistake means adding a new migration that corrects it.
2. **Every migration reversible.** `downgrade()` fully undoes `upgrade()`. A bare `pass` or
   `raise NotImplementedError` downgrade fails the gate. Single exemption: the root baseline
   revision (`4a07fe3a8e54`), which creates nothing.
3. **Autogenerate reviewed by hand.** `alembic revision --autogenerate` output is a draft.
   Review every operation, name every constraint through the metadata convention, verify the
   downgrade, and run the round trip locally before committing.
4. **Exactly one head at all times.** A branch in the revision graph is merged immediately;
   CI and the gate reject multiple heads.

## Constraint naming

All constraints are named by the `MetaData` naming convention in `backend/app/db.py`
(binding; do not change existing entries):

| Kind | Template |
|---|---|
| primary key | `pk_%(table_name)s` |
| foreign key | `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` |
| unique | `uq_%(table_name)s_%(column_0_name)s` |
| check | `ck_%(table_name)s_%(constraint_name)s` |
| index | `ix_%(column_0_label)s` |

Never pass an explicit unrelated name; let the convention generate it so autogenerate stays
deterministic and downgrades can always drop what upgrades created.

## Authoring workflow

1. Change or add models against `app.db.Base`.
2. `uv run alembic revision --autogenerate -m "short_slug"` (file names follow
   `{rev}_{slug}.py`).
3. Hand-review per convention 3; keep the file ruff-clean.
4. Round trip locally: `upgrade head`, `downgrade base`, `upgrade head` against a scratch
   database.
5. Run the gate; the migration suite verifies chain integrity, reversibility, the empty
   autogenerate diff, and constraint names.
