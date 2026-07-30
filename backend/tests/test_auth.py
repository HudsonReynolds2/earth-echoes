"""Gate 6: local accounts and sessions (task E0.6).

Phase-doc acceptance: passwords never logged or returned; sessions expire;
tests cover login, logout, expiry, and wrong-password paths. Runs against a
real ephemeral Postgres migrated to head (rule R0: never self-skips), plus
unit checks on the signing and hashing primitives.
"""

import logging
import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient

from app.auth.cookies import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    sign_session_id,
    unsign_session_id,
)
from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.main import API_PREFIX, create_app
from app.models import User, UserSession, utcnow
from app.settings import Settings

# Generated per run: no password-like literal may be committed, even as a
# fixture; the repo's own secret scanner enforces this (R2, DECISIONS D18).
PASSWORD = f"pw-{uuid.uuid4().hex}"
EMAIL = "owner@example.com"


@pytest.fixture(scope="module")
def pg_url():
    """Shared by every DB-backed suite via import; each importing module gets
    its own migrated throwaway instance (conftest.ephemeral_postgres)."""
    with ephemeral_postgres() as url:
        yield url


@pytest.fixture(scope="module")
def app(pg_url):
    return create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate6-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )


@pytest.fixture(scope="module")
def seeded_user(app):
    factory = app.state.session_factory
    with factory() as db:
        user = User(email=EMAIL, password_hash=hash_password(PASSWORD))
        db.add(user)
        db.commit()
        return user.id


@pytest.fixture()
def client(app, seeded_user):
    return TestClient(app, raise_server_exceptions=False)


def _login(client: TestClient, password: str = PASSWORD):
    return client.post(f"{API_PREFIX}/auth/login", json={"email": EMAIL, "password": password})


# --- login paths -----------------------------------------------------------


@pytest.mark.integration
def test_login_success_sets_both_cookies_and_returns_no_secrets(client):
    response = _login(client)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == EMAIL and body["is_active"] is True
    assert "password" not in response.text and "hash" not in body
    raw = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in raw if c.startswith(f"{SESSION_COOKIE}="))
    csrf_cookie = next(c for c in raw if c.startswith(f"{CSRF_COOKIE}="))
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie
    assert "HttpOnly" not in csrf_cookie  # double-submit: JS must read it (D4)


@pytest.mark.integration
def test_wrong_password_and_unknown_email_are_indistinguishable(client):
    wrong = _login(client, password="not-the-password")
    unknown = client.post(
        f"{API_PREFIX}/auth/login", json={"email": "ghost@example.com", "password": "x"}
    )
    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
    assert wrong.json() == unknown.json(), "responses must not reveal which field was wrong"


@pytest.mark.integration
def test_password_never_appears_in_logs_or_responses(client, caplog):
    with caplog.at_level(logging.DEBUG):
        response = _login(client)
    assert PASSWORD not in response.text
    for record in caplog.records:
        assert PASSWORD not in record.getMessage(), "password leaked into a log record"


@pytest.mark.integration
def test_inactive_user_cannot_login(client, app):
    factory = app.state.session_factory
    with factory() as db:
        user = User(email="inactive@example.com", password_hash=hash_password(PASSWORD))
        user.is_active = False
        db.add(user)
        db.commit()
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": "inactive@example.com", "password": PASSWORD}
    )
    assert response.status_code == 401


# --- session lifecycle -----------------------------------------------------


@pytest.mark.integration
def test_me_requires_a_session_and_rejects_tampering(client):
    assert client.get(f"{API_PREFIX}/auth/me").status_code == 401
    _login(client)
    assert client.get(f"{API_PREFIX}/auth/me").status_code == 200
    tampered = client.cookies[SESSION_COOKIE][:-2] + "zz"
    client.cookies.set(SESSION_COOKIE, tampered)
    assert client.get(f"{API_PREFIX}/auth/me").status_code == 401


@pytest.mark.integration
def test_logout_requires_csrf_and_revokes_immediately(client):
    _login(client)
    no_csrf = client.post(f"{API_PREFIX}/auth/logout")
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "forbidden"

    csrf = client.cookies[CSRF_COOKIE]
    response = client.post(f"{API_PREFIX}/auth/logout", headers={CSRF_HEADER: csrf})
    assert response.status_code == 204
    # The revoked session is dead even if the cookie were replayed.
    assert client.get(f"{API_PREFIX}/auth/me").status_code == 401


@pytest.mark.integration
def test_expired_session_is_rejected(client, app):
    _login(client)
    assert client.get(f"{API_PREFIX}/auth/me").status_code == 200
    factory = app.state.session_factory
    with factory() as db:
        for session in db.query(UserSession).all():
            session.expires_at = utcnow()
        db.commit()
    assert client.get(f"{API_PREFIX}/auth/me").status_code == 401


# --- primitives (unit) -----------------------------------------------------


def test_cookie_signing_round_trip_and_tamper_rejection():
    signed = sign_session_id("session-abc", "secret-1")
    assert unsign_session_id(signed, "secret-1") == "session-abc"
    assert unsign_session_id(signed + "x", "secret-1") is None
    assert unsign_session_id(signed, "secret-2") is None
    assert unsign_session_id("no-signature-here", "secret-1") is None


def test_password_hashing_is_argon2id_and_verifies():
    digest = hash_password(PASSWORD)
    assert digest.startswith("$argon2id$")
    assert PASSWORD not in digest
    assert verify_password(digest, PASSWORD) is True
    assert verify_password(digest, "wrong") is False
    assert needs_rehash(digest) is False
