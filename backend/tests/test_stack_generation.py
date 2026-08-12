"""E5.9: credentials generated, stored and committed before anything renders.

The phase document's acceptance is three claims:

1. A fault injected after credential generation and before commit leaves zero
   rows and zero secrets.
2. Every credential is `secrets.token_urlsafe`-grade and none is derived from
   the deployment's slug, id or name — asserted by generating two stacks for
   ONE deployment and diffing every credential.
3. Regenerating rotates every credential and the old ones are gone from
   SecretStore.

The first is the one worth the most care, because `SecretStore.put` commits on
its own session (E0.11) and so does not roll back with the row transaction.
"Zero secrets" is therefore a property of the compensation path, not something
the database gives for free, and a test that only checked rows would pass while
leaking every credential.
"""

import uuid

import pytest
from conftest import ephemeral_postgres, make_kek

from app.db import create_session_factory
from app.models import Deployment, Organization
from app.secrets import SecretStore
from app.services import store
from app.services.schemas import secret_name
from app.services.stackgen import (
    STACK_SECRET_ITEMS,
    StackNotGenerated,
    generate_stack,
    load_generated_stack,
    stack_secret_name,
)

SLUG = "redwood-coast"


@pytest.fixture
def env():
    """A migrated database with one deployment, plus a SecretStore on it."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        secret_store = SecretStore(factory, make_kek())
        with factory() as db:
            org = Organization(name="e59-org")
            db.add(org)
            db.flush()
            deployment = Deployment(
                id=uuid.uuid4(), organization_id=org.id, name="Redwood Coast", slug=SLUG
            )
            db.add(deployment)
            db.commit()
            db.refresh(deployment)
            yield db, secret_store, deployment, factory


def _all_secret_names(factory) -> set[str]:
    """Every secret name in the store, read straight out of the table.

    Enumerated rather than probed by name, because the point of acceptance 1
    is that NOTHING was left behind — a check that only looked for the names
    it expected would miss the one the generator wrote and forgot to track.
    """
    from sqlalchemy import select

    from app.models import Secret

    with factory() as db:
        return set(db.scalars(select(Secret.name)))


def _credentials(secret_store, factory, deployment_id) -> dict[str, str]:
    """Every stored secret's VALUE, by name. The diffing acceptance needs the
    plaintexts, which is the one place in the suite that reads them all."""
    return {
        name: secret_store.get(name)
        for name in _all_secret_names(factory)
        if str(deployment_id) in name
    }


# --- Acceptance 1: a fault before commit leaves nothing behind --------------


def test_a_fault_before_commit_leaves_zero_rows_and_zero_secrets(env, monkeypatch):
    """**The acceptance, and the one the database does not give for free.**

    `SecretStore.put` commits on its own session, so a rolled-back row
    transaction leaves live ciphertext unless the generator compensates. The
    fault is injected at the last write before the commit.
    """
    db, secret_store, deployment, factory = env
    assert _all_secret_names(factory) == set(), "guard: the store starts empty"

    boom = RuntimeError("injected after generation, before commit")
    real_upsert = store.upsert_service
    calls = {"n": 0}

    def exploding_upsert(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:  # after mqtt and influx, mid-write
            raise boom
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(store, "upsert_service", exploding_upsert)

    with pytest.raises(RuntimeError, match="injected after generation"):
        generate_stack(db, secret_store, deployment)

    assert store.load_services(db, deployment.id) == [], "rows survived a failed generation"
    assert _all_secret_names(factory) == set(), (
        "secrets survived a failed generation — SecretStore.put commits on its own "
        "session, so the generator has to delete what it wrote"
    )


def test_a_failed_regeneration_leaves_the_previous_stack_intact(env, monkeypatch):
    """Compensation must not be destructive. An operator whose rotation fails
    still has a working deployment — the old credentials keep working, because
    the failure path deletes only what THIS call wrote."""
    db, secret_store, deployment, factory = env
    generate_stack(db, secret_store, deployment)
    before = _credentials(secret_store, factory, deployment.id)
    assert before

    real_upsert = store.upsert_service
    calls = {"n": 0}

    def exploding_upsert(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("rotation failed")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(store, "upsert_service", exploding_upsert)
    with pytest.raises(RuntimeError, match="rotation failed"):
        generate_stack(db, secret_store, deployment)

    # The rows are still there and still load.
    assert {row.service_key for row in store.load_services(db, deployment.id)} >= {
        "mqtt",
        "influx",
    }
    after = _credentials(secret_store, factory, deployment.id)
    assert set(after) == set(before), "a failed rotation removed a name the old stack needs"
    # Values, not just names. Regeneration overwrites the SAME deterministic
    # names, so a compensation that deleted them — or left the half-written new
    # ones in place — would leave the operator with a deployment whose broker,
    # Influx and Grafana credentials no longer match anything they hold.
    assert after == before, (
        "a failed rotation changed a live credential; the previous stack must be "
        "byte-for-byte intact, because nothing has been redistributed to the devices"
    )


# --- Acceptance 2: real entropy, derived from nothing -----------------------


def test_two_generations_for_one_deployment_share_no_credential(env):
    """**The acceptance, diffed.** Every value must differ between two runs for
    the same deployment: anything derived from the slug, the id or the name
    would repeat, and a credential a reader of the inventory can reconstruct is
    not a credential."""
    db, secret_store, deployment, factory = env

    generate_stack(db, secret_store, deployment)
    first = _credentials(secret_store, factory, deployment.id)
    generate_stack(db, secret_store, deployment)
    second = _credentials(secret_store, factory, deployment.id)

    assert set(first) == set(second), "guard: the two runs wrote the same names"
    shared = {name for name in first if first[name] == second[name]}
    assert shared == set(), f"these credentials did not change between generations: {shared}"


def test_no_credential_contains_the_deployments_own_identifiers(env):
    """The other half of "derived from nothing": not just different between
    runs, but not built out of public facts either."""
    db, secret_store, deployment, factory = env
    generate_stack(db, secret_store, deployment)

    identifiers = {deployment.slug, str(deployment.id), deployment.name, "redwood", "Redwood"}
    for name, value in _credentials(secret_store, factory, deployment.id).items():
        if name.endswith(("ca_cert", "server_cert", "server_key", "ca_key")):
            continue  # certificates legitimately carry hostnames
        if name.endswith("password_hash"):
            continue  # hashes are of the credential, not the identifiers
        for identifier in identifiers:
            assert identifier not in value, f"{name} is derived from {identifier!r}"


def test_generated_credentials_have_real_length(env):
    """`secrets.token_urlsafe(32)` grade. A short credential on a service
    exposed to a deployment's whole network is the failure this guards."""
    db, secret_store, deployment, factory = env
    generate_stack(db, secret_store, deployment)
    token = secret_store.get(secret_name(deployment.id, "influx", "token"))
    assert len(token) >= 40


# --- Acceptance 3: regeneration rotates everything --------------------------


def test_regenerating_rotates_the_broker_and_prometheus_hashes_too(env):
    """The hashes are stored, not recomputed (D130), so rotation has to
    overwrite them explicitly — a rotation that changed the password but left
    the hash would ship a bundle whose broker rejects the new credential."""
    db, secret_store, deployment, factory = env
    generate_stack(db, secret_store, deployment)
    old_broker = secret_store.get(stack_secret_name(deployment.id, "broker_admin_password_hash"))
    old_prom = secret_store.get(stack_secret_name(deployment.id, "prometheus_password_hash"))

    generate_stack(db, secret_store, deployment)
    assert (
        secret_store.get(stack_secret_name(deployment.id, "broker_admin_password_hash"))
        != old_broker
    )
    assert (
        secret_store.get(stack_secret_name(deployment.id, "prometheus_password_hash")) != old_prom
    )


def test_the_stored_hash_actually_verifies_the_stored_password(env):
    """Generated together and stored separately, so nothing but a test proves
    they match. A mismatch is a broker that refuses the platform's own
    account — visible only when a device fails to connect."""
    import bcrypt

    db, secret_store, deployment, factory = env
    generate_stack(db, secret_store, deployment)

    prom_password = secret_store.get(
        secret_name(deployment.id, "prometheus", "remote_write_password")
    )
    prom_hash = secret_store.get(stack_secret_name(deployment.id, "prometheus_password_hash"))
    assert bcrypt.checkpw(prom_password.encode(), prom_hash.encode())


# --- The rows the generation writes -----------------------------------------


def test_every_service_row_lands_untested(env):
    """Spec 16.5 gates bundle generation on verification. A generated stack
    starting anywhere but `untested` would let a stack vouch for itself."""
    db, secret_store, deployment, _ = env
    generate_stack(db, secret_store, deployment, include_object_storage=True)
    rows = store.load_services(db, deployment.id)
    assert len(rows) == 5
    assert {row.status for row in rows} == {"untested"}
    assert {row.last_tested_at for row in rows} == {None}


def test_object_storage_is_written_only_when_asked_for(env):
    """D123: object storage is conditionally required, so a deployment that
    does not upload raw audio must not carry an s3 row waiting to be
    verified."""
    db, secret_store, deployment, _ = env
    generate_stack(db, secret_store, deployment, include_object_storage=False)
    assert store.load_service(db, deployment.id, "s3") is None


def test_regenerating_without_object_storage_removes_its_row(env):
    """`roll_up` reads rows, not intentions: a left-behind s3 row would keep
    `services_status` waiting on a service the operator just turned off."""
    db, secret_store, deployment, _ = env
    generate_stack(db, secret_store, deployment, include_object_storage=True)
    assert store.load_service(db, deployment.id, "s3") is not None

    generate_stack(db, secret_store, deployment, include_object_storage=False)
    assert store.load_service(db, deployment.id, "s3") is None


def test_no_row_carries_a_plaintext_credential(env):
    """Rule R2. The rows hold NAMES; the values are SecretStore's alone."""
    db, secret_store, deployment, factory = env
    generate_stack(db, secret_store, deployment, include_object_storage=True)
    values = set(_credentials(secret_store, factory, deployment.id).values())

    for row in store.load_services(db, deployment.id):
        blob = f"{row.config}{row.secret_names}{row.username}{row.ca_cert_pem}"
        for value in values:
            if value in (row.ca_cert_pem or ""):
                continue  # the CA cert is on the row on purpose: it is public
            assert value not in blob, f"{row.service_key} carries a credential in its columns"


def test_the_ca_certificate_is_on_the_row_and_the_private_key_is_not(env):
    """The trust anchor is public and the platform must have it to verify TLS;
    the private key is the broker's and belongs in SecretStore."""
    db, secret_store, deployment, factory = env
    generate_stack(db, secret_store, deployment)
    mqtt = store.load_service(db, deployment.id, "mqtt")
    assert mqtt is not None
    assert "BEGIN CERTIFICATE" in (mqtt.ca_cert_pem or "")

    ca_key = secret_store.get(stack_secret_name(deployment.id, "ca_key"))
    assert "PRIVATE KEY" in ca_key
    assert ca_key not in (mqtt.ca_cert_pem or "")


# --- Re-reading it back -----------------------------------------------------


def test_reloading_produces_the_same_spec_as_generation_returned(env):
    """The property fixed choice 7 rests on: a download re-renders from the
    STORED rows, so what generation returned and what a later request loads
    must be the same inputs."""
    db, secret_store, deployment, _ = env
    generated = generate_stack(db, secret_store, deployment, include_object_storage=True)
    reloaded = load_generated_stack(db, secret_store, deployment)
    assert generated.spec == reloaded.spec
    assert generated.env == reloaded.env


def test_loading_a_stack_that_was_never_generated_is_an_error_not_an_empty_bundle(env):
    """An operator who never generated a stack must get a refusal, not a
    bundle full of blanks that fails mysteriously at `docker compose up`."""
    db, secret_store, deployment, _ = env
    with pytest.raises(StackNotGenerated):
        load_generated_stack(db, secret_store, deployment)


def test_every_enumerated_stack_secret_is_actually_written(env):
    """`STACK_SECRET_ITEMS` drives rotation and deletion. An item the
    generator writes but the list omits is a credential rotation misses."""
    db, secret_store, deployment, _ = env
    generate_stack(db, secret_store, deployment)
    for item in STACK_SECRET_ITEMS:
        assert secret_store.exists(stack_secret_name(deployment.id, item)), item
