"""Database metadata foundations (task E0.2).

The naming convention makes Alembic autogenerate deterministic and gives every
constraint a name a downgrade can drop, which the append-only, reversible
migration policy depends on (docs/migration-conventions.md). Binding for all
later phases; do not change existing entries.
"""

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Declarative base every model in every phase inherits from."""

    metadata = metadata


def create_session_factory(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    """Engine plus session factory; create_app owns one per application."""
    engine = create_engine(database_url, pool_pre_ping=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)
