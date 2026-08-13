"""E5.10: the platform mints its own Grafana service account, against a real one.

**Why this exists at all.** Every other credential in this phase is generated
before the service that will accept it is running — fixed choice 7 — and that
is what lets a bundle be rendered from stored rows and downloaded twice
byte-identically. Grafana cannot take part: token values are issued by Grafana
at runtime and shown once, so a stack the platform generated cannot be handed a
token in advance. The only credential a fresh Grafana accepts up front is
`GF_SECURITY_ADMIN_USER` / `_PASSWORD`.

So the admin account is a bootstrap, spent once, and the durable credential is a
scoped service account token. That is what `conftest._mint_grafana_token` has
been doing to build the rig since E5.4d — its docstring says "exactly as the
platform will" — and until this unit the platform did not. E5.9 stored the
ADMIN PASSWORD under the `service_account_token` name and E5.4d sent it as a
bearer token; Grafana answered 401 and a generated stack could never reach
`verified`. E5.10's keystone is what found it.

The rig's Grafana is the real one, so "creates a service account" here means
Grafana's own listing changed.
"""

import asyncio
import uuid

import pytest
from conftest import RIG_PASSWORD, ephemeral_postgres, make_kek

from app.db import create_session_factory
from app.models import Deployment, Organization
from app.secrets import SecretStore
from app.services import provision, store
from app.services.clients.grafana import (
    SERVICE_ACCOUNT_NAME,
    SERVICE_ACCOUNT_ROLE,
    GrafanaAdminClient,
    GrafanaClient,
)
from app.services.clients.httpbase import ServiceDialError
from app.services.schemas import secret_name

pytestmark = pytest.mark.integration

#: The rig's Grafana admin account. `GF_SECURITY_ADMIN_USER` is unset on that
#: container, so it is Grafana's own default.
ADMIN_USER = "admin"


def admin_client(rig, password: str = RIG_PASSWORD) -> GrafanaAdminClient:
    return GrafanaAdminClient(base_url=rig.grafana.url, username=ADMIN_USER, password=password)


def mint(rig) -> str:
    """Run the bootstrap the way `provision` does and return the token."""
    client = admin_client(rig)

    async def go() -> str:
        async with client.session() as session:
            account_id = await client.ensure_service_account(session)
            return await client.issue_token(session, account_id)

    return asyncio.run(go())


def account_ids(rig) -> list[int]:
    """Every service account Grafana holds under our name, asked of Grafana."""
    client = admin_client(rig)

    async def go() -> list[int]:
        async with client.session() as session:
            url = client.endpoint("/api/serviceaccounts/search")
            response = await session.get(url, params={"query": SERVICE_ACCOUNT_NAME})
            payload = response.json()
            return [
                account["id"]
                for account in payload.get("serviceAccounts", [])
                if account.get("name") == SERVICE_ACCOUNT_NAME
            ]

    return asyncio.run(go())


def datasources_with(rig, token: str) -> int:
    """Reading datasources is one of the two things the platform needs the
    token for, and the one that told us the admin password was the wrong
    credential — it answered 401."""
    client = GrafanaClient(base_url=rig.grafana.url, token=token)

    async def go() -> int:
        async with client.session() as session:
            return len(await client.datasources(session))

    return asyncio.run(go())


# --- The bootstrap itself ---------------------------------------------------


def test_the_admin_account_can_mint_a_token_the_platform_can_then_use(rig):
    """The whole claim, end to end: an admin username and password go in, and a
    credential that reads `/api/datasources` comes out.

    Both halves matter. `Bearer <admin password>` answers 401 on this exact
    endpoint — that was the defect — so a test that only proved the admin
    account works would not have caught it.
    """
    token = mint(rig)
    assert token
    assert token != RIG_PASSWORD, "the minted credential must not be the admin password"
    assert datasources_with(rig, token) >= 0


def test_minting_twice_leaves_exactly_one_service_account(rig):
    """Lookup-then-decide, the rule `ensure_contact_point` already follows.

    An operator brings a stack down and up, or rotates; a bootstrap that POSTed
    blindly would leave a drawer of identical `echoes-platform` accounts, each
    with Admin on their Grafana, and no way to tell which is live.
    """
    mint(rig)
    first = account_ids(rig)
    mint(rig)
    second = account_ids(rig)
    assert len(first) == 1
    assert second == first, "a second bootstrap created a second service account"


def test_a_new_token_stops_the_previous_one_working(rig):
    """**The property rotation rests on.** Re-minting revokes what was there
    first, so a rotation whose whole purpose is that the old credential stops
    being accepted actually achieves it. Grafana has no endpoint that reads a
    token value back, so revoking by id at issue time is the only moment this
    can be done."""
    old = mint(rig)
    assert datasources_with(rig, old) >= 0
    new = mint(rig)
    assert new != old
    assert datasources_with(rig, new) >= 0
    with pytest.raises(ServiceDialError) as caught:
        datasources_with(rig, old)
    assert caught.value.failure.kind == "auth"


def test_the_service_account_holds_the_admin_role(rig):
    """Editor is not enough for `/api/v1/provisioning`, which is where the
    contact point is registered — and the failure would be a 403 at the last
    step of onboarding rather than at the first."""
    mint(rig)
    client = admin_client(rig)

    async def role() -> str:
        async with client.session() as session:
            url = client.endpoint("/api/serviceaccounts/search")
            response = await session.get(url, params={"query": SERVICE_ACCOUNT_NAME})
            accounts = response.json()["serviceAccounts"]
            return next(a for a in accounts if a["name"] == SERVICE_ACCOUNT_NAME)["role"]

    assert asyncio.run(role()) == SERVICE_ACCOUNT_ROLE


def test_a_wrong_admin_password_fails_with_a_remedy_naming_the_env_file(rig):
    """The operator-facing half. A generated stack's admin credentials live in
    the bundle's `.env`, and an operator who changed the password in Grafana
    has to be told where the platform expects to find the new one."""
    client = admin_client(rig, password="not-the-admin-password")

    async def go():
        async with client.session() as session:
            await client.ensure_service_account(session)

    with pytest.raises(ServiceDialError) as caught:
        asyncio.run(go())
    failure = caught.value.failure
    assert failure.kind == "auth"
    assert ".env" in failure.remedy
    assert failure.remedy.strip()


def test_no_admin_password_reaches_a_failure_message(rig):
    """The phase's definition-of-done rule, applied to the one credential this
    module handles. A remedy is operator-facing text and an admin password is
    not part of it."""
    client = admin_client(rig, password=RIG_PASSWORD + "-wrong")

    async def go():
        async with client.session() as session:
            await client.ensure_service_account(session)

    with pytest.raises(ServiceDialError) as caught:
        asyncio.run(go())
    blob = repr(caught.value.failure)
    assert RIG_PASSWORD not in blob


# --- When the bootstrap runs, and when it must not --------------------------


def _row(config: dict, secret_names: dict):
    """A stand-in for a `deployment_service` row, for the pure predicate."""

    class Row:
        pass

    row = Row()
    row.config = config
    row.secret_names = secret_names
    return row


def test_a_row_with_an_admin_account_and_no_token_needs_the_bootstrap():
    assert provision.needs_grafana_bootstrap(
        _row({"admin_username": "eoe"}, {"admin_password": "deployment:x:grafana_admin_password"})
    )


def test_a_row_that_already_has_a_token_is_left_alone():
    """**Deliberately not "the stored token does not work".**

    A revoked token, or one an operator pasted wrongly, is a failure the tester
    should report with its remedy. Minting a silent replacement would hide the
    operator's mistake and issue fresh credentials on every failed test.
    """
    assert not provision.needs_grafana_bootstrap(
        _row(
            {"admin_username": "eoe"},
            {"admin_password": "deployment:x:pw", "service_account_token": "deployment:x:token"},
        )
    )


def test_a_bring_your_own_grafana_never_reaches_the_bootstrap():
    """Path A: the operator pasted a service account token and supplied no
    admin account. Nothing is created on their Grafana, which is E5.4d's
    recorded acceptance and stays true for them."""
    assert not provision.needs_grafana_bootstrap(
        _row({}, {"service_account_token": "deployment:x:token"})
    )
    assert not provision.needs_grafana_bootstrap(_row({}, {}))
    assert not provision.needs_grafana_bootstrap(None)


def test_an_admin_username_with_no_password_is_not_a_bootstrap():
    """A half-filled form is not an instruction to go and create credentials."""
    assert not provision.needs_grafana_bootstrap(_row({"admin_username": "eoe"}, {}))


# --- The orchestration, against the database --------------------------------


def test_the_minted_token_is_stored_and_named_on_the_row(rig):
    """`ensure_grafana_service_account` writes the token under the SAME field
    an operator's pasted token uses, so that once the bootstrap has run a
    generated stack and a bring-your-own Grafana are indistinguishable to every
    reader downstream."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        secret_store = SecretStore(factory, make_kek())
        with factory() as db:
            org = Organization(name="bootstrap-org")
            db.add(org)
            db.flush()
            deployment = Deployment(
                id=uuid.uuid4(), organization_id=org.id, name="Bootstrap", slug="bootstrap"
            )
            db.add(deployment)
            db.commit()

            password_name = secret_name(deployment.id, "grafana", "admin_password")
            secret_store.put(password_name, RIG_PASSWORD)
            store.upsert_service(
                db,
                deployment.id,
                "grafana",
                config={"base_url": rig.grafana.url, "admin_username": ADMIN_USER},
                secret_names={"admin_password": password_name},
            )
            db.commit()

            stored_name = asyncio.run(
                provision.ensure_grafana_service_account(db, secret_store, deployment.id)
            )
            db.commit()

            assert stored_name == secret_name(deployment.id, "grafana", "service_account_token")
            row = store.load_service(db, deployment.id, "grafana")
            assert row.secret_names["service_account_token"] == stored_name
            # The admin password is still there — a rotation needs it again.
            assert row.secret_names["admin_password"] == password_name

            token = secret_store.get(stored_name)
            assert token != RIG_PASSWORD
            assert datasources_with(rig, token) >= 0

            # Idempotent: the row now has a token, so a second call is a no-op
            # rather than a fresh service account on the operator's Grafana.
            assert (
                asyncio.run(
                    provision.ensure_grafana_service_account(db, secret_store, deployment.id)
                )
                is None
            )


def test_a_bootstrap_failure_never_raises_out_of_the_batch_entry_point(rig):
    """E5.3's rule: one service's problem leaves the other four verdicts real.
    A Grafana that refuses the admin account becomes that service's auth
    failure with its remedy, not a 500 that hides the other four."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        secret_store = SecretStore(factory, make_kek())
        with factory() as db:
            org = Organization(name="bootstrap-fail-org")
            db.add(org)
            db.flush()
            deployment = Deployment(
                id=uuid.uuid4(), organization_id=org.id, name="Fail", slug="bootstrap-fail"
            )
            db.add(deployment)
            db.commit()

            password_name = secret_name(deployment.id, "grafana", "admin_password")
            secret_store.put(password_name, "not-the-admin-password")
            store.upsert_service(
                db,
                deployment.id,
                "grafana",
                config={"base_url": rig.grafana.url, "admin_username": ADMIN_USER},
                secret_names={"admin_password": password_name},
            )
            db.commit()

            assert (
                asyncio.run(provision.ensure_service_credentials(db, secret_store, deployment.id))
                == []
            )
            row = store.load_service(db, deployment.id, "grafana")
            assert "service_account_token" not in row.secret_names
