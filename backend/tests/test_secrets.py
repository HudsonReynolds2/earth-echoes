"""Gate 11: platform secrets envelope encryption (task E0.11; spec 12.4).

Phase-doc acceptance: round-trip and rotation tests pass; grepping test logs
for a known plaintext secret finds nothing; the interface names its E4/E5
consumers. Also proves ciphertext-only at rest and that error paths never
carry plaintext.
"""

import logging
import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from sqlalchemy import select

import app.secrets as secrets_module
from app.db import create_session_factory
from app.models import Secret
from app.secrets import SecretStore, SecretStoreError

# Generated per run (R2/D18): committed secret-like literals are forbidden.
PLAINTEXT = f"value-{uuid.uuid4().hex}"


@pytest.fixture(scope="module")
def factory():
    with ephemeral_postgres() as url:
        _, session_factory = create_session_factory(url)
        yield session_factory


@pytest.fixture(scope="module")
def store(factory):
    # One KEK for the module, mirroring production: a table only ever holds
    # rows wrapped by the store's current KEK (rotation swaps all of them).
    return SecretStore(factory, make_kek())


# --- construction fails loudly on a bad KEK --------------------------------


def test_invalid_kek_rejected(factory):
    with pytest.raises(SecretStoreError, match="base64"):
        SecretStore(factory, "not-base64!!!")
    with pytest.raises(SecretStoreError, match="32 bytes"):
        SecretStore(factory, "c2hvcnQ=")  # "short"


# --- round trip and lifecycle ----------------------------------------------


@pytest.mark.integration
def test_round_trip_and_upsert(store):
    store.put("probe:round-trip", PLAINTEXT)
    assert store.get("probe:round-trip") == PLAINTEXT
    replacement = f"value-{uuid.uuid4().hex}"
    store.put("probe:round-trip", replacement)
    assert store.get("probe:round-trip") == replacement


@pytest.mark.integration
def test_exists_delete_and_unknown_name(store):
    assert store.exists("probe:lifecycle") is False
    store.put("probe:lifecycle", PLAINTEXT)
    assert store.exists("probe:lifecycle") is True
    store.delete("probe:lifecycle")
    assert store.exists("probe:lifecycle") is False
    with pytest.raises(SecretStoreError, match="probe:lifecycle"):
        store.get("probe:lifecycle")


# --- ciphertext-only at rest ------------------------------------------------


@pytest.mark.integration
def test_only_ciphertext_reaches_the_database(store, factory):
    store.put("probe:at-rest", PLAINTEXT)
    with factory() as db:
        row = db.scalar(select(Secret).where(Secret.name == "probe:at-rest"))
    assert row is not None
    assert PLAINTEXT.encode() not in row.ciphertext
    assert PLAINTEXT.encode() not in row.wrapped_dek
    assert row.kek_fingerprint and len(row.kek_fingerprint) == 16


# --- rotation ---------------------------------------------------------------


@pytest.mark.integration
def test_rotation_rewraps_without_touching_values():
    # Isolated database: rotation re-wraps every row in the table, so this
    # test owns its whole table (one KEK at a time, as in production).
    with ephemeral_postgres() as url:
        _, isolated_factory = create_session_factory(url)
        old_kek, new_kek = make_kek(), make_kek()
        store = SecretStore(isolated_factory, old_kek)
        store.put("probe:rotate-a", PLAINTEXT)
        store.put("probe:rotate-b", f"value-{uuid.uuid4().hex}")

        rewrapped = store.rotate_kek(new_kek)
        assert rewrapped == 2
        # The rotated store and a fresh store on the new KEK both read fine.
        assert store.get("probe:rotate-a") == PLAINTEXT
        assert SecretStore(isolated_factory, new_kek).get("probe:rotate-a") == PLAINTEXT
        # A store still holding the old KEK cannot unwrap, and says why
        # without leaking anything.
        with pytest.raises(SecretStoreError, match="KEK mismatch") as excinfo:
            SecretStore(isolated_factory, old_kek).get("probe:rotate-a")
        assert PLAINTEXT not in str(excinfo.value)


# --- tampering --------------------------------------------------------------


@pytest.mark.integration
def test_tampered_ciphertext_fails_authentication_without_leaking(store, factory):
    store.put("probe:tamper", PLAINTEXT)
    with factory() as db:
        row = db.scalar(select(Secret).where(Secret.name == "probe:tamper"))
        row.ciphertext = bytes([row.ciphertext[0] ^ 0xFF]) + row.ciphertext[1:]
        db.commit()
    with pytest.raises(SecretStoreError, match="authentication") as excinfo:
        store.get("probe:tamper")
    assert PLAINTEXT not in str(excinfo.value)


# --- the acceptance grep: plaintext never in logs ---------------------------


@pytest.mark.integration
def test_plaintext_never_appears_in_logs(store, caplog):
    with caplog.at_level(logging.DEBUG):
        store.put("probe:log-hygiene", PLAINTEXT)
        assert store.get("probe:log-hygiene") == PLAINTEXT
    for record in caplog.records:
        assert PLAINTEXT not in record.getMessage(), "secret plaintext leaked into a log"


# --- the docstring contract -------------------------------------------------


def test_interface_names_its_consumers():
    doc = secrets_module.__doc__ or ""
    assert "E4" in doc and "E5" in doc, "consumers must be named (phase-doc acceptance)"
    assert "8.4" in doc, "must disambiguate from the device-facing scheme"
    store_doc = SecretStore.__doc__ or ""
    assert "envelope" in (doc + store_doc).lower()
