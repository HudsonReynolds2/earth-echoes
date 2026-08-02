"""Naming rules for hierarchy entities (tasks E1.2, E1.4; spec 4.2/4.3, 7.2).

Slug: the deployment slug keys the MQTT topic namespace ({dep}, spec 7.2), so
generation is deterministic and the format matches the database CHECK exactly.
MAC: normalized once, at the API boundary, to the uppercase colon-separated
form the listener primary key CHECK-constrains (D31).
"""

import re
import unicodedata
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import Deployment, Listener

NAME_MAX = 200

_SLUG_SQUASH = re.compile(r"[^a-z0-9]+")
_MAC_SEPARATORS = re.compile(r"[:\-.\s]")
_MAC_HEX = re.compile(r"^[0-9A-F]{12}$")

SLUG_MAX = 63


def slugify(name: str) -> str:
    """Deterministic slug from a display name: NFKD-strip to ASCII, lowercase,
    squash every non-alphanumeric run to one hyphen, trim, cap at 63."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = _SLUG_SQUASH.sub("-", ascii_name.lower()).strip("-")
    return slug[:SLUG_MAX].rstrip("-")


def next_free_slug(db: Session, base: str) -> str:
    """`base`, then `base-2`, `base-3`, ... - the first not already taken.
    Slugs are globally unique (spec 7.2 topic namespace)."""
    if not base:
        raise AppError("validation_error", "name yields an empty slug", status_code=422)
    candidate = base
    counter = 2
    while db.scalar(select(Deployment.id).where(Deployment.slug == candidate)) is not None:
        suffix = f"-{counter}"
        candidate = f"{base[: SLUG_MAX - len(suffix)]}{suffix}"
        counter += 1
    return candidate


def next_free_name(db: Session, deployment_id: uuid.UUID, base: str) -> str:
    """The spec 4.3 auto-suffix ladder for listener names within a deployment:
    `base`, then `base-2`, `base-3`, ... - the first not already taken. Only
    ever applied on an explicit request parameter, never silently (E1.4)."""
    candidate = base
    counter = 2
    while (
        db.scalar(
            select(Listener.mac).where(
                Listener.deployment_id == deployment_id, Listener.name == candidate
            )
        )
        is not None
    ):
        suffix = f"-{counter}"
        candidate = f"{base[: NAME_MAX - len(suffix)]}{suffix}"
        counter += 1
    return candidate


def normalize_mac(raw: str) -> str:
    """Uppercase colon-separated AA:BB:CC:DD:EE:FF, accepting colon/hyphen/dot
    separated or bare-hex input. Anything else is the envelope's 422."""
    cleaned = _MAC_SEPARATORS.sub("", raw).upper()
    if not _MAC_HEX.match(cleaned):
        raise AppError("validation_error", f"invalid MAC address {raw!r}", status_code=422)
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
