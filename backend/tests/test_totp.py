"""Gate 10: optional TOTP (task E0.10; spec 12.2).

Phase-doc acceptance: an enrolled owner must pass TOTP at login; unenrolled
users are unaffected. Off by default. The secret lives only in SecretStore
(totp:{user_id}); it is returned exactly once at enrollment and never after.
"""

import pyotp
import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import RoleAssignment, User
from app.settings import Settings

OWNER_EMAIL = "totp-owner@example.com"
PLAIN_EMAIL = "totp-plain@example.com"


@pytest.fixture(scope="module")
def totp_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate10-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        owner = User(email=OWNER_EMAIL, password_hash=hash_password(PASSWORD))
        owner.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
        plain = User(email=PLAIN_EMAIL, password_hash=hash_password(PASSWORD))
        db.add_all([owner, plain])
        db.commit()
    return app


def _login(app, email: str, totp_code: str | None = None):
    client = TestClient(app, raise_server_exceptions=False)
    payload = {"email": email, "password": PASSWORD}
    if totp_code is not None:
        payload["totp_code"] = totp_code
    return client, client.post(f"{API_PREFIX}/auth/login", json=payload)


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


@pytest.fixture(scope="module")
def enrolled_secret(totp_app) -> str:
    """Walks the full enrollment: enroll returns the secret once, a wrong
    confirm code is rejected, the right one flips totp_enabled."""
    client, response = _login(totp_app, OWNER_EMAIL)
    assert response.status_code == 200

    enroll = client.post(f"{API_PREFIX}/auth/totp/enroll", headers=_csrf(client))
    assert enroll.status_code == 200
    body = enroll.json()
    secret = body["secret"]
    assert secret and "otpauth://" in body["otpauth_url"]

    wrong = client.post(
        f"{API_PREFIX}/auth/totp/confirm", json={"code": "000000"}, headers=_csrf(client)
    )
    assert wrong.status_code == 422

    confirm = client.post(
        f"{API_PREFIX}/auth/totp/confirm",
        json={"code": pyotp.TOTP(secret).now()},
        headers=_csrf(client),
    )
    assert confirm.status_code == 200
    assert confirm.json() == {"totp_enabled": True}
    return secret


# --- acceptance: enrolled owner must pass TOTP at login ---------------------


@pytest.mark.integration
def test_enrolled_owner_login_requires_a_code(totp_app, enrolled_secret):
    _, missing = _login(totp_app, OWNER_EMAIL)
    assert missing.status_code == 401
    assert missing.json()["error"]["detail"] == {"totp_required": True}

    _, wrong = _login(totp_app, OWNER_EMAIL, totp_code="000000")
    assert wrong.status_code == 401
    assert wrong.json()["error"]["detail"] is None  # indistinguishable from bad creds

    client, right = _login(totp_app, OWNER_EMAIL, totp_code=pyotp.TOTP(enrolled_secret).now())
    assert right.status_code == 200
    assert client.get(f"{API_PREFIX}/auth/me").status_code == 200


@pytest.mark.integration
def test_unenrolled_users_are_unaffected(totp_app, enrolled_secret):
    client, response = _login(totp_app, PLAIN_EMAIL)
    assert response.status_code == 200
    assert client.get(f"{API_PREFIX}/auth/me").status_code == 200


# --- off by default, secret hygiene, re-enrollment guards -------------------


@pytest.mark.integration
def test_secret_lives_in_secret_store_and_never_leaks_after_enrollment(totp_app, enrolled_secret):
    from sqlalchemy import select

    from app.api.totp import secret_name
    from app.models import User as UserModel

    with totp_app.state.session_factory() as db:
        owner_id = db.scalar(select(UserModel.id).where(UserModel.email == OWNER_EMAIL))
    assert totp_app.state.secret_store.get(secret_name(owner_id)) == enrolled_secret

    client, response = _login(totp_app, OWNER_EMAIL, totp_code=pyotp.TOTP(enrolled_secret).now())
    assert enrolled_secret not in response.text
    assert enrolled_secret not in client.get(f"{API_PREFIX}/auth/me").text


@pytest.mark.integration
def test_enroll_again_conflicts_once_enabled(totp_app, enrolled_secret):
    client, _ = _login(totp_app, OWNER_EMAIL, totp_code=pyotp.TOTP(enrolled_secret).now())
    assert client.post(f"{API_PREFIX}/auth/totp/enroll", headers=_csrf(client)).status_code == 409


@pytest.mark.integration
def test_confirm_without_enrollment_conflicts(totp_app):
    client, _ = _login(totp_app, PLAIN_EMAIL)
    response = client.post(
        f"{API_PREFIX}/auth/totp/confirm", json={"code": "123456"}, headers=_csrf(client)
    )
    assert response.status_code == 409


@pytest.mark.integration
def test_enrollment_mutations_require_csrf(totp_app):
    client, _ = _login(totp_app, PLAIN_EMAIL)
    assert client.post(f"{API_PREFIX}/auth/totp/enroll").status_code == 403


@pytest.mark.integration
def test_enrollment_and_enable_are_audited(totp_app, enrolled_secret):
    from sqlalchemy import select

    from app.models import AuditLog

    with totp_app.state.session_factory() as db:
        actions = set(db.scalars(select(AuditLog.action).where(AuditLog.action.like("auth.totp%"))))
    assert {"auth.totp_enroll", "auth.totp_enabled"} <= actions
