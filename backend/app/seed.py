"""Dev seed (task E0.12): fresh environment to logged-in owner in ONE command.

    uv run python -m app.seed

Runs migrations to head, then creates the initial organization-wide owner
account. The password is generated, printed EXACTLY ONCE to stdout, and never
stored anywhere except as an Argon2id hash (phase-0 acceptance; rule R2).
Refuses to run if an org-wide owner already exists. Override the email with
EOE_SEED_OWNER_EMAIL (default owner@example.com).
"""

import os
import secrets
import sys
from pathlib import Path

from alembic.config import Config
from sqlalchemy import select

from alembic import command
from app.audit import record_audit
from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.db import create_session_factory
from app.models import RoleAssignment, User
from app.settings import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_EMAIL = "owner@example.com"


def migrate_to_head(database_url: str) -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["configure_logger"] = False
    os.environ.setdefault("DATABASE_URL", database_url)
    command.upgrade(config, "head")


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]  # resolves from env (D5)
    email = os.environ.get("EOE_SEED_OWNER_EMAIL", DEFAULT_EMAIL)

    print("running migrations to head ...")
    migrate_to_head(settings.database_url)

    _, factory = create_session_factory(settings.database_url)
    with factory() as db:
        existing = db.scalar(
            select(User)
            .join(RoleAssignment)
            .where(
                RoleAssignment.role == Role.OWNER.value,
                RoleAssignment.deployment_id.is_(None),
            )
        )
        if existing is not None:
            print(
                f"an organization-wide owner already exists ({existing.email}); refusing to seed",
                file=sys.stderr,
            )
            return 1

        password = secrets.token_urlsafe(16)
        owner = User(email=email, password_hash=hash_password(password))
        owner.role_assignments.append(RoleAssignment(role=Role.OWNER.value, deployment_id=None))
        db.add(owner)
        db.flush()
        record_audit(
            db,
            action="user.create",
            entity_type="user",
            entity_id=str(owner.id),
            actor_user_id=None,  # system bootstrap
            detail={"email": email, "roles": [Role.OWNER.value], "seed": True},
        )
        db.commit()

    print()
    print("=" * 62)
    print("  initial owner created — record these credentials NOW;")
    print("  the password is not stored and will never be shown again")
    print("=" * 62)
    print(f"  email:    {email}")
    print(f"  password: {password}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
