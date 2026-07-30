"""Session-cookie signing (task E0.6; decision D1).

The cookie value is `<session_id>.<hmac>`: an opaque random id plus an
HMAC-SHA256 signature under EOE_SESSION_SECRET. The signature stops cookie
forgery without a DB hit; validity (expiry, revocation) lives on the session
row. Stdlib only; no signing dependency.
"""

import hashlib
import hmac
import secrets

SESSION_COOKIE = "eoe_session"
CSRF_COOKIE = "eoe_csrf"
CSRF_HEADER = "X-CSRF-Token"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _signature(session_id: str, secret: str) -> str:
    return hmac.new(secret.encode(), session_id.encode(), hashlib.sha256).hexdigest()


def sign_session_id(session_id: str, secret: str) -> str:
    return f"{session_id}.{_signature(session_id, secret)}"


def unsign_session_id(cookie_value: str, secret: str) -> str | None:
    """Return the session id, or None for a missing/tampered signature."""
    session_id, separator, signature = cookie_value.rpartition(".")
    if not separator or not session_id:
        return None
    if not hmac.compare_digest(signature, _signature(session_id, secret)):
        return None
    return session_id
