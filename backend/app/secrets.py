"""Platform-side secrets envelope encryption (task E0.11; spec 12.4).

THE ONLY PATH FOR SECRETS AT REST (rule R2). Scheme, exactly spec 12.4:
a fresh 256-bit data key (DEK) encrypts each secret value with AES-256-GCM;
the platform key-encryption key (KEK, from EOE_KEK now, a secret manager
after E8.1) wraps the DEK; only ciphertext reaches Postgres. Rotation
re-wraps every DEK under a new KEK without touching the secret values
(rotate_kek), which is what E8.1 automates.

Consumers: E4 stores device-facing bundle secrets (WiFi PSK, stream key)
here before their separate firmware-KEK/DEK encryption (spec 8.4) is applied
at export; E5 stores deployment service credentials (broker, Influx,
Prometheus, Grafana, S3). E0.10 stores TOTP secrets. Plaintext never appears
in logs, API responses, or exceptions raised by this module.

This is deliberately NOT the device-facing scheme of spec 8.4: that scheme
(firmware-embedded KEK, per-Pod DEK, on-card ciphertext) belongs to E4. The
two nest; they do not compete.
"""

import base64
import hashlib
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Secret

_KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretStoreError(Exception):
    """Raised for unknown names or an unusable KEK; never carries plaintext."""


def _decode_kek(kek_base64: str) -> bytes:
    try:
        kek = base64.b64decode(kek_base64, validate=True)
    except Exception as error:
        raise SecretStoreError("EOE_KEK is not valid base64") from error
    if len(kek) != _KEY_BYTES:
        raise SecretStoreError(f"EOE_KEK must decode to {_KEY_BYTES} bytes, got {len(kek)}")
    return kek


def _fingerprint(kek: bytes) -> str:
    return hashlib.sha256(kek).hexdigest()[:16]


class SecretStore:
    """Envelope-encrypting store over the `secret` table.

    put/get/delete by name; names are namespaced by convention
    (`totp:{user_id}`, `deployment:{id}:influx_token`, ...). Constructed from
    the settings KEK: SecretStore(session_factory, settings.kek).
    """

    def __init__(self, session_factory: sessionmaker[Session], kek_base64: str) -> None:
        self._factory = session_factory
        self._kek = _decode_kek(kek_base64)
        self._kek_fp = _fingerprint(self._kek)

    def put(self, name: str, plaintext: str) -> None:
        """Create or replace the named secret; a fresh DEK every write."""
        dek = os.urandom(_KEY_BYTES)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), None)
        dek_nonce = os.urandom(_NONCE_BYTES)
        wrapped_dek = AESGCM(self._kek).encrypt(dek_nonce, dek, None)
        with self._factory() as db:
            row = db.scalar(select(Secret).where(Secret.name == name))
            if row is None:
                row = Secret(id=uuid.uuid4(), name=name)
                db.add(row)
            row.wrapped_dek = wrapped_dek
            row.dek_nonce = dek_nonce
            row.ciphertext = ciphertext
            row.nonce = nonce
            row.kek_fingerprint = self._kek_fp
            db.commit()

    def get(self, name: str) -> str:
        with self._factory() as db:
            row = db.scalar(select(Secret).where(Secret.name == name))
            if row is None:
                raise SecretStoreError(f"no secret named {name!r}")
            return self._decrypt(row)

    def exists(self, name: str) -> bool:
        with self._factory() as db:
            return db.scalar(select(Secret.id).where(Secret.name == name)) is not None

    def delete(self, name: str) -> None:
        with self._factory() as db:
            row = db.scalar(select(Secret).where(Secret.name == name))
            if row is not None:
                db.delete(row)
                db.commit()

    def rotate_kek(self, new_kek_base64: str) -> int:
        """Re-wrap every DEK under a new KEK (spec 12.4 rotation; E8.1
        automates this against a secret manager). Secret values are untouched.
        Returns the number of rows re-wrapped; the store uses the new KEK
        afterwards."""
        new_kek = _decode_kek(new_kek_base64)
        new_fp = _fingerprint(new_kek)
        rewrapped = 0
        with self._factory() as db:
            for row in db.scalars(select(Secret)):
                dek = self._unwrap(row)
                dek_nonce = os.urandom(_NONCE_BYTES)
                row.wrapped_dek = AESGCM(new_kek).encrypt(dek_nonce, dek, None)
                row.dek_nonce = dek_nonce
                row.kek_fingerprint = new_fp
                rewrapped += 1
            db.commit()
        self._kek = new_kek
        self._kek_fp = new_fp
        return rewrapped

    def _unwrap(self, row: Secret) -> bytes:
        try:
            return AESGCM(self._kek).decrypt(row.dek_nonce, row.wrapped_dek, None)
        except InvalidTag as error:
            raise SecretStoreError(
                f"cannot unwrap secret {row.name!r}: KEK mismatch "
                f"(row wrapped by {row.kek_fingerprint}, store holds {self._kek_fp})"
            ) from error

    def _decrypt(self, row: Secret) -> str:
        dek = self._unwrap(row)
        try:
            plaintext = AESGCM(dek).decrypt(row.nonce, row.ciphertext, None)
        except InvalidTag as error:
            raise SecretStoreError(f"secret {row.name!r} failed authentication") from error
        return plaintext.decode("utf-8")
