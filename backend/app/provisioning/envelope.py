"""The device-facing firmware envelope (task E4.5; spec 8.4; phase-4 fixed
choice 3; DECISIONS D166, D175).

**This is a wire contract firmware must parse** (INTERFACES, "Owned by E4").
Changing a field name, the encoding, the AAD, or the algorithm strands every
card already written, so the format is versioned by `ENVELOPE_FORMAT` and
pinned by tests.

## The scheme (spec 8.4)

- **KEK** (`EOE_FIRMWARE_KEK`): one static 32-byte key shared by every
  Listener's firmware in the Organization. It only ever wraps DEKs.
- **DEK**: 32 fresh random bytes **per Pod per export**. It encrypts the PSK and
  the stream key directly.
- Everything is **AES-256-GCM**, the algorithm ESP32 runs in hardware through
  mbedTLS and the one `app.secrets` already uses for the platform's own storage.

The two envelope schemes nest; they do not compete. SecretStore protects these
same values at rest under the PLATFORM KEK. This envelope protects them in
transit to a device under the FIRMWARE KEK. A value is plaintext only in memory,
between `effective_resolved` and `seal`.

## On-card encoding

Every sealed blob is `base64(nonce || ciphertext || tag)`: a 12-byte random
nonce, then AESGCM's output (ciphertext with its 16-byte tag appended). The
additional authenticated data binds each blob to its role, so a blob cannot
be moved to another field and still authenticate:

- the wrapped DEK uses AAD `b"eoe-dek:v1"`;
- a sealed value uses AAD `b"eoe-secret:v1:" + <catalog key>`, for example
  `b"eoe-secret:v1:network.wifi_password"`.

## Key custody stays open

Spec 17 item 7 is NOT closed by this module. The KEK comes from an environment
variable for development; custody, backup and Organization-wide rotation gate
production use. Losing this key strands provisioning of new Pods across the
whole Organization. `key_version` exists so a future rotation can say which KEK
a card needs; it does not make rotation possible on its own.
"""

import base64
import hashlib
import os
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Bumped only by an incompatible change to the encoding below.
ENVELOPE_FORMAT = 1
ALGORITHM = "AES-256-GCM"

KEY_BYTES = 32
NONCE_BYTES = 12

DEK_AAD = b"eoe-dek:v1"
SECRET_AAD_PREFIX = b"eoe-secret:v1:"


class EnvelopeError(Exception):
    """An unusable firmware key, or a blob that failed authentication. Never
    carries key material or plaintext."""


def _fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


@dataclass(frozen=True)
class FirmwareKey:
    """The firmware KEK and its version. `key` is `repr=False` (the D66
    precedent): this object sits on `app.state` and a stray `%r` must not
    print it."""

    key: bytes = field(repr=False)
    key_version: int

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.key)

    @classmethod
    def from_base64(cls, value: str | None, key_version: int) -> "FirmwareKey":
        """Validate `EOE_FIRMWARE_KEK` the way `EOE_KEK` is validated: fail
        loudly, naming the variable and never echoing its value."""
        if not value:
            raise EnvelopeError(
                "EOE_FIRMWARE_KEK is required: base64 of exactly 32 random bytes "
                "(openssl rand -base64 32)"
            )
        try:
            key = base64.b64decode(value, validate=True)
        except Exception as error:
            raise EnvelopeError("EOE_FIRMWARE_KEK is not valid base64") from error
        if len(key) != KEY_BYTES:
            raise EnvelopeError(
                f"EOE_FIRMWARE_KEK must decode to {KEY_BYTES} bytes, got {len(key)} "
                f"(fingerprint {_fingerprint(key)})"
            )
        if key_version < 1:
            raise EnvelopeError(f"EOE_FIRMWARE_KEY_VERSION must be >= 1, got {key_version}")
        return cls(key=key, key_version=key_version)


def _encrypt(key: bytes, plaintext: bytes, aad: bytes) -> str:
    nonce = os.urandom(NONCE_BYTES)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plaintext, aad)).decode("ascii")


def _decrypt(key: bytes, blob: str, aad: bytes) -> bytes:
    try:
        raw = base64.b64decode(blob, validate=True)
    except Exception as error:
        raise EnvelopeError("sealed value is not valid base64") from error
    if len(raw) <= NONCE_BYTES:
        raise EnvelopeError("sealed value is too short to hold a nonce and a tag")
    try:
        return AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad)
    except InvalidTag as error:
        # Tampered, truncated, moved to another field, or the wrong key: GCM
        # cannot say which, and that is the property that matters.
        raise EnvelopeError("sealed value failed authentication") from error


def new_dek() -> bytes:
    """A fresh data key. One per Pod per export (spec 8.4)."""
    return os.urandom(KEY_BYTES)


def wrap_dek(firmware_key: FirmwareKey, dek: bytes) -> str:
    return _encrypt(firmware_key.key, dek, DEK_AAD)


def unwrap_dek(firmware_key: FirmwareKey, wrapped: str) -> bytes:
    """What firmware does first at boot. Used here by tests and by any future
    rotation path, never by generation."""
    return _decrypt(firmware_key.key, wrapped, DEK_AAD)


def seal(dek: bytes, catalog_key: str, plaintext: str) -> str:
    return _encrypt(dek, plaintext.encode("utf-8"), SECRET_AAD_PREFIX + catalog_key.encode())


def unseal(dek: bytes, catalog_key: str, blob: str) -> str:
    return _decrypt(dek, blob, SECRET_AAD_PREFIX + catalog_key.encode()).decode("utf-8")


@dataclass(frozen=True)
class SealedSecrets:
    """The `secrets:` block of one card: everything firmware needs, given its
    KEK, to recover the Pod's PSK and stream key. `values` maps the catalog
    key's short name (`wifi_password`, `stream_key`) to its sealed blob; a key
    with no value (an OPEN network's password) is simply absent."""

    key_version: int
    wrapped_dek: str
    values: dict[str, str]

    def as_block(self) -> dict[str, object]:
        """The YAML mapping, in a fixed field order."""
        return {
            "format": ENVELOPE_FORMAT,
            "algorithm": ALGORITHM,
            "key_version": self.key_version,
            "wrapped_dek": self.wrapped_dek,
            **dict(sorted(self.values.items())),
        }


def seal_secrets(
    firmware_key: FirmwareKey, dek: bytes, plaintexts: dict[str, str | None]
) -> SealedSecrets:
    """Seal each present secret under `dek` and wrap `dek` under the KEK.

    `plaintexts` maps full catalog keys (`network.wifi_password`) to plaintext
    or None. None and empty values are left out rather than sealed, so an OPEN
    network's card carries no password field at all.
    """
    values = {
        key.split(".", 1)[1]: seal(dek, key, value) for key, value in plaintexts.items() if value
    }
    return SealedSecrets(
        key_version=firmware_key.key_version, wrapped_dek=wrap_dek(firmware_key, dek), values=values
    )
