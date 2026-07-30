"""Audit write hook (task E0.8; spec 14.1).

Every mutation endpoint calls record_audit inside its own transaction: the
audit row commits or rolls back atomically with the mutation it describes.
The request id comes from the middleware contextvar, correlating the row with
response headers and log records. Universal DoD: every new mutation in every
later phase audits.
"""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.middleware import request_id_var
from app.models import AuditLog


def record_audit(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_user_id: uuid.UUID | None = None,
    scope: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """Stage an audit row on the caller's session; the caller's commit seals
    both the mutation and its audit trail together. Never commits itself."""
    row = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        scope=scope,
        detail=detail,
        request_id=request_id_var.get(),
    )
    db.add(row)
    return row
