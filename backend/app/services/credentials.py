"""Per-device broker credentials: minting, revocation, and the E4 seam
(task E5.6; spec 7.1, 7.2, 16.4; phase-5 fixed choices 4 and 6).

Every Aggregator dials its deployment's broker as itself, with its own login
and its own ACL cut to its own subtree. This module is where those logins come
from and where they are destroyed.

## The seam, and which way it points

`phase-4-provisioning.md` originally had E4.6 declare `BrokerCredentialProvider`
and E5.6 add an implementation. E4 was never built, so the dependency reversed
(addendum PHASE4-2-01, DECISIONS D105): **this module DEFINES the protocol and
ships both implementations**, and E4.6's remaining work is to import it, pick
one, and flip `EOE_BOOTSTRAP_CREDENTIALS`.

* `DynsecCredentialProvider` — the real one. Fixed choice 4 makes Mosquitto's
  dynamic security plugin required for v1, so a per-device login is a
  `createClient` on a broker rather than a line appended to a file.
* `DevBrokerCredentialProvider` — reads the accounts `app.devbroker` already
  generated for the development stack. It mints nothing: the dev broker uses
  `acl_file` and a password file, which `mosquitto_passwd` and a SIGHUP own,
  and inventing a second writer for them is how the two drift apart.

## Why the ACL grants are not written down here

They are `devbroker.aggregator_acl_grants`, rendered into the plugin's
vocabulary by `dynsec_role_acls` below. There are now two authorization
backends reading the same spec 7.2 Direction column, and the failure mode of
two literal readings is not a cosmetic diff — it is one missing line letting an
Aggregator publish to its own `desired` topic, manufacture agreement with
itself, and defeat drift detection. One list, two renderers, one test asserting
both against it (E5.6 acceptance).

## Why a mint dials its own connection

The same three reasons `app/services/dynsec.py` gives for the probe, and they
are not stylistic: `MqttClientManager`'s subscription set is fixed before
`start()` (D64) and `$CONTROL` is not in it; the manager only knows deployments
that already have a row; and correlating a reply wants a fresh session so a
stale answer cannot be read as this call's. `MqttServiceClient` is reused rather
than a fourth way of dialling a broker, so D65's pinned-CA rule holds here
identically.

## Revocation is allowed to be slow, and is not allowed to be skipped

Deleting an Aggregator while its broker is unreachable must not stop the
operator, and must not leave a decommissioned Pi holding a working login. So
the row goes to `revoke_pending`, the delete proceeds, and
`drain_pending_revocations` retries on the worker's sweep until the broker
confirms. Owner's decision, 2026-08-12 (DECISIONS D121, project-changes #27);
the alternative on the table was a 503 that refused the delete.
"""

import contextlib
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiomqtt
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.controlplane.broker import BrokerCoordinates, load_broker_coordinates
from app.devbroker import AclGrant, aggregator_acl_grants, device_username, load_manifest
from app.models import BrokerCredential
from app.secrets import SecretStore
from app.services import dynsec
from app.services.clients.mqtt import MqttDialError, MqttServiceClient

logger = logging.getLogger(__name__)

#: Bytes of entropy behind a generated device password, matching
#: `devbroker.plan_accounts`. These are machine-held and never typed.
PASSWORD_ENTROPY_BYTES = 24


class CredentialError(Exception):
    """Minting or revoking failed in a way the caller must surface.

    Deliberately not an `AppError`: this module is called from the API *and*
    from the worker's sweep, and only one of those has an error envelope.
    """


class BrokerUnreachable(CredentialError):
    """The broker could not be dialled or would not answer.

    Separated from `CredentialError` because the two have different answers:
    an unreachable broker is retried (`revoke_pending`), while a plugin that
    refused a command is a configuration fault an operator has to fix.
    """


def secret_name(deployment_id: object, aggregator_uuid: str) -> str:
    """The SecretStore name for one device's broker password.

    Inside the `deployment:{id}:*` namespace the INTERFACES SecretStore section
    documents, with an interior colon — the same shape E2.2's config names
    already use (D51).
    """
    return f"deployment:{deployment_id}:device:{aggregator_uuid}"


def dynsec_role_name(aggregator_uuid: str) -> str:
    """The dynsec role carrying one device's grants.

    A role per device rather than one shared role, because the grants are
    topic-scoped to `aggregator_root(slug, uuid)` — a shared role would have
    to name every device's subtree and would hand each of them all the others.
    """
    return f"device-{aggregator_uuid}"


def dynsec_role_acls(grants: Sequence[AclGrant]) -> list[dict[str, Any]]:
    """Render the ONE grant list into the dynamic security plugin's vocabulary.

    The plugin splits what `acl_file` calls `read` into two acltypes and they
    are both needed: `subscribePattern` decides whether the SUBSCRIBE is
    accepted, `publishClientReceive` whether a matching message is actually
    delivered. Granting only the first produces a device that subscribes
    successfully and then receives nothing — indistinguishable, from the
    device's side, from a platform that never published.

    `write` is `publishClientSend` alone. There is deliberately no
    `unsubscribePattern` grant: with `defaultACLAccess.unsubscribe` true a
    client may always drop its own subscriptions, and a device unsubscribing
    from its own topics harms nobody but itself.
    """
    acls: list[dict[str, Any]] = []
    for grant in grants:
        if grant.access == "read":
            acls.append({"acltype": "subscribePattern", "topic": grant.topic, "allow": True})
            acls.append({"acltype": "publishClientReceive", "topic": grant.topic, "allow": True})
        elif grant.access == "write":
            acls.append({"acltype": "publishClientSend", "topic": grant.topic, "allow": True})
        else:  # pragma: no cover - AclGrant is constructed only by devbroker
            raise ValueError(f"unknown ACL access {grant.access!r}")
    return acls


@dataclass(frozen=True)
class DeviceCredential:
    """One device's broker login, as the provider hands it back.

    `password` is `repr=False` and `__str__` names only the device, following
    `BrokerCoordinates` and `MqttServiceClient` (D66): this object is logged
    and audited by callers, and a later `%r` must not be able to leak it.
    """

    username: str
    password: str = field(repr=False)

    def __str__(self) -> str:
        return f"broker credential for {self.username}"


@runtime_checkable
class BrokerCredentialProvider(Protocol):
    """**The E4.6 seam.** How the platform obtains and destroys one device's
    broker login, without the caller knowing which broker backend is in play.

    Async because the real implementation is a round trip over MQTT.
    `coordinates` rather than a `deployment_id` because the caller has already
    resolved and authorized the deployment, and because a provider that
    re-queried would need a database session it has no other use for.
    """

    async def mint(
        self, coordinates: BrokerCoordinates, aggregator_uuid: str
    ) -> DeviceCredential: ...

    async def revoke(self, coordinates: BrokerCoordinates, aggregator_uuid: str) -> None: ...


# --- The dynsec provider ----------------------------------------------------


def _is_absent(error: dynsec.DynsecError) -> bool:
    """Whether the plugin refused because the thing is already gone.

    Matched on the plugin's own wording, lowercased, and deliberately loosely:
    a `deleteClient` for a client that does not exist is the state revocation
    is trying to reach, and treating it as a failure would make a retry that
    succeeded look like one that had not.
    """
    text = error.error.lower()
    return "not found" in text or "does not exist" in text


class DynsecCredentialProvider:
    """Mint and revoke through `$CONTROL/dynamic-security/v1`.

    **Every operation is idempotent by construction**, because the alternative
    is a half-provisioned device. A mint deletes any existing client and role
    for this `aggregator_uuid` before creating them, which makes a rotation and
    a first mint the same code path and makes a retry after a partial failure
    safe. The delete tolerates "not found" and nothing else.
    """

    def __init__(self, *, timeout: float = dynsec.DEFAULT_RESPONSE_TIMEOUT) -> None:
        self._timeout = timeout

    async def mint(self, coordinates: BrokerCoordinates, aggregator_uuid: str) -> DeviceCredential:
        username = device_username(aggregator_uuid)
        password = secrets.token_urlsafe(PASSWORD_ENTROPY_BYTES)
        role = dynsec_role_name(aggregator_uuid)
        acls = dynsec_role_acls(aggregator_acl_grants(coordinates.slug, aggregator_uuid))

        async with _admin_client(coordinates) as client:
            await _require_admin(client, coordinates, timeout=self._timeout)
            await self._drop(client, username, role)
            await dynsec.call(
                client,
                [
                    {"command": "createRole", "rolename": role, "acls": acls},
                    {
                        "command": "createClient",
                        "username": username,
                        "password": password,
                        "roles": [{"rolename": role}],
                    },
                ],
                timeout=self._timeout,
                subscribed=True,
            )
        logger.info("minted a broker credential for %s on the %s", aggregator_uuid, coordinates)
        return DeviceCredential(username=username, password=password)

    async def revoke(self, coordinates: BrokerCoordinates, aggregator_uuid: str) -> None:
        username = device_username(aggregator_uuid)
        role = dynsec_role_name(aggregator_uuid)
        async with _admin_client(coordinates) as client:
            await _require_admin(client, coordinates, timeout=self._timeout)
            await self._drop(client, username, role)
        logger.info("revoked the broker credential for %s on the %s", aggregator_uuid, coordinates)

    async def _drop(self, client: aiomqtt.Client, username: str, role: str) -> None:
        """Delete the client and its role, tolerating either being absent.

        Two calls rather than one publish, because `dynsec.call` raises on the
        FIRST error in a batch and would abandon the second command — so a
        client that had already been deleted would leave its role behind
        forever. Each is sent and judged on its own.
        """
        for command in (
            {"command": "deleteClient", "username": username},
            {"command": "deleteRole", "rolename": role},
        ):
            try:
                await dynsec.call(client, [command], timeout=self._timeout, subscribed=True)
            except dynsec.DynsecError as error:
                if not _is_absent(error):
                    raise CredentialError(
                        f"the broker refused {command['command']}: {error.error}"
                    ) from error
            except TimeoutError as error:
                raise BrokerUnreachable(str(error)) from error


@contextlib.asynccontextmanager
async def _admin_client(coordinates: BrokerCoordinates) -> AsyncIterator[aiomqtt.Client]:
    """A short-lived connection on the deployment's platform account.

    Built through `MqttServiceClient` so TLS is `broker.py::tls_context` and
    the pinned-CA rule of D65 is the same one the control plane dials with,
    rather than a fourth way of dialling a broker in this codebase.

    A dial failure becomes `BrokerUnreachable` — including a refused CONNACK,
    because a platform account the broker no longer accepts is, for the purpose
    of "can this revocation be completed now", the same as a broker that is
    down: retrying is the right response, and the operator's own remedy for
    bad credentials already reached them through the E5.4a tester.
    """
    client = MqttServiceClient(
        deployment_id=coordinates.deployment_id,
        deployment_slug=coordinates.slug,
        host=coordinates.host,
        port=coordinates.port,
        username=coordinates.username,
        password=coordinates.password,
        tls_enabled=coordinates.tls_enabled,
        ca_cert_pem=coordinates.ca_cert_pem,
    )
    try:
        async with client.connect() as connected:
            yield connected
    except MqttDialError as error:
        raise BrokerUnreachable(
            f"could not reach the {coordinates}: {error.failure.detail}"
        ) from error


async def _require_admin(
    client: aiomqtt.Client, coordinates: BrokerCoordinates, *, timeout: float
) -> None:
    """Refuse to proceed unless the plugin is present and we may drive it.

    Without this a `createClient` against a plugin-less broker times out and
    reports "the plugin did not answer", which is true and useless. The probe's
    verdicts carry the remedy an operator can act on (`absent` says how to
    enable dynsec, `denied` says which role to grant), so they are what gets
    surfaced. Fixed choice 4: a broker without dynsec cannot be used, and this
    is the sentence that says so at the moment it matters.
    """
    probe = await dynsec.probe(client, timeout=timeout)
    if probe.usable:
        return
    raise CredentialError(
        f"cannot mint credentials on the {coordinates}: {probe.detail}. {probe.remedy}"
    )


# --- The dev-stack provider -------------------------------------------------


class DevBrokerCredentialProvider:
    """The accounts `app.devbroker` already wrote, read back.

    **It mints nothing and revokes nothing**, and that is the honest shape
    rather than a limitation to apologise for. The development broker
    authenticates from a Mosquitto password file and authorizes from an
    `acl_file`; both are generated wholesale by `app.devbroker` and reloaded
    with a SIGHUP. A provider that appended to them would be a second writer of
    two files whose whole design is that one pass rewrites them together, and
    the drift would show up as a device that cannot log in.

    So `revoke` is a no-op that says so in the log, and the caller's row still
    reaches `revoked`: on the dev stack the credential's real lifecycle is
    "until the next `python -m app.devbroker` run", which rotates every
    password anyway.
    """

    def __init__(self, manifest_dir: Path | None = None) -> None:
        self._manifest_dir = manifest_dir

    async def mint(self, coordinates: BrokerCoordinates, aggregator_uuid: str) -> DeviceCredential:
        manifest = (
            load_manifest(self._manifest_dir) if self._manifest_dir is not None else load_manifest()
        )
        accounts: list[dict[str, Any]] = manifest.get("accounts", [])
        for account in accounts:
            if account.get("aggregator_uuid") == aggregator_uuid:
                return DeviceCredential(
                    username=str(account["username"]), password=str(account["password"])
                )
        raise CredentialError(
            f"the dev broker manifest has no account for aggregator {aggregator_uuid}; "
            "re-run 'uv run python -m app.devbroker' after creating it, then "
            "'docker compose restart mosquitto'"
        )

    async def revoke(self, coordinates: BrokerCoordinates, aggregator_uuid: str) -> None:
        logger.info(
            "dev broker: not revoking %s (its accounts are rewritten wholesale by "
            "app.devbroker; nothing here is a second writer of the password file)",
            aggregator_uuid,
        )


# --- Coordinates for one deployment -----------------------------------------


def coordinates_for(
    session_factory: sessionmaker[Session],
    secret_store: SecretStore,
    deployment_id: uuid.UUID,
) -> BrokerCoordinates:
    """This deployment's broker coordinates, or a `CredentialError` saying why not.

    Filters `load_broker_coordinates` rather than writing a second query,
    deliberately: that function carries D64's and D109's skip rules — an
    unreadable secret, a row missing its connection columns — and a second
    loader would be a second, quietly different answer to "is this row usable".
    """
    for coordinates in load_broker_coordinates(session_factory, secret_store):
        if coordinates.deployment_id == deployment_id:
            return coordinates
    raise CredentialError(
        "this deployment has no usable MQTT service row; configure the broker under "
        "Services and verify it before minting device credentials"
    )


# --- The rows -----------------------------------------------------------------


def load_credential(
    db: Session, deployment_id: uuid.UUID, aggregator_uuid: str
) -> BrokerCredential | None:
    return db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.deployment_id == deployment_id,
            BrokerCredential.aggregator_uuid == aggregator_uuid,
        )
    )


async def mint_credential(
    db: Session,
    secret_store: SecretStore,
    provider: BrokerCredentialProvider,
    coordinates: BrokerCoordinates,
    aggregator_uuid: str,
) -> BrokerCredential:
    """Mint on the broker, then record it. **In that order.**

    A row written before the broker agreed would claim a login that does not
    exist, and the device it was generated for would fail to connect with
    nothing in the platform suggesting why. The reverse leak — a broker client
    with no row — is recoverable, because a re-mint deletes and recreates it.

    Does not commit: the caller owns the transaction, like every other service
    in this codebase. The SecretStore put lands immediately (D51), which is
    safe for the same reason it always is — a ciphertext no row points at is
    unreachable.
    """
    credential = await provider.mint(coordinates, aggregator_uuid)
    name = secret_name(coordinates.deployment_id, aggregator_uuid)
    secret_store.put(name, credential.password)

    row = load_credential(db, coordinates.deployment_id, aggregator_uuid)
    if row is None:
        row = BrokerCredential(
            deployment_id=coordinates.deployment_id, aggregator_uuid=aggregator_uuid
        )
        db.add(row)
    row.username = credential.username
    row.password_secret_name = name
    row.state = "minted"
    row.revoked_at = None
    db.flush()
    return row


async def revoke_credential(
    db: Session,
    provider: BrokerCredentialProvider,
    coordinates: BrokerCoordinates,
    row: BrokerCredential,
) -> BrokerCredential:
    """Destroy the login on the broker and mark the row.

    An unreachable broker leaves the row `revoke_pending` rather than raising:
    the caller is usually an operator deleting inventory, and blocking that on
    somebody else's outage is the failure mode the owner rejected. A plugin
    that answered and REFUSED is a different thing and does raise — that is a
    configuration fault, not a transient one, and retrying it forever would
    hide it.
    """
    try:
        await provider.revoke(coordinates, row.aggregator_uuid)
    except BrokerUnreachable as error:
        logger.warning(
            "could not revoke %s on the %s (%s); leaving it revoke_pending for the sweep",
            row.aggregator_uuid,
            coordinates,
            error,
        )
        row.state = "revoke_pending"
        row.revoked_at = None
        db.flush()
        return row
    row.state = "revoked"
    row.revoked_at = datetime.now(UTC)
    db.flush()
    return row


async def revoke_for_deleted_aggregator(
    db: Session,
    session_factory: sessionmaker[Session],
    secret_store: SecretStore,
    provider: BrokerCredentialProvider,
    deployment_id: uuid.UUID,
    aggregator_uuid: str,
) -> str:
    """Destroy a departing device's login, and never stop the delete.

    **The single call `app/api/aggregators.py::delete_aggregator` makes**, kept
    to one line there on purpose: that endpoint is E1-owned, and the smaller
    the edit the easier it is to see that nothing else about deleting an
    Aggregator changed.

    Returns what happened, for the caller's audit detail:
    `none` (there was never a credential), `revoked`, or `revoke_pending`.
    **Every failure mode returns rather than raises.** A broker that is down, a
    deployment whose broker row has already been removed, a plugin that refuses
    — none of them may prevent an operator from removing a device from
    inventory. What each of them leaves behind is a row the sweep will keep
    working on, which is the outcome the state exists to guarantee.

    The `broker_credential` row is deliberately NOT deleted with the device;
    see the model docstring.
    """
    row = load_credential(db, deployment_id, aggregator_uuid)
    if row is None:
        return "none"
    try:
        coordinates = coordinates_for(session_factory, secret_store, deployment_id)
    except CredentialError:
        logger.warning(
            "aggregator %s is being deleted and its deployment has no usable broker row; "
            "its credential stays revoke_pending",
            aggregator_uuid,
        )
        row.state = "revoke_pending"
        row.revoked_at = None
        db.flush()
        return row.state
    try:
        await revoke_credential(db, provider, coordinates, row)
    except CredentialError:
        # The plugin answered and refused. Recorded with a stack trace because
        # it needs an operator, and left pending because the credential is
        # still live and forgetting it is the one outcome that is not allowed.
        logger.exception(
            "the broker refused to revoke %s during an aggregator delete", aggregator_uuid
        )
        row.state = "revoke_pending"
        row.revoked_at = None
        db.flush()
    return row.state


@dataclass
class RevocationSweepReport:
    """What one pass of `drain_pending_revocations` did.

    Shaped like `runner.py`'s own sweep reports (`changed` decides whether the
    worker logs a line) so a third sweep on that loop reads like the two
    already there.
    """

    revoked: int = 0
    still_pending: int = 0
    failed: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.revoked or self.failed)

    def __str__(self) -> str:
        return f"{self.revoked} revoked, {self.still_pending} still pending, {self.failed} refused"


async def drain_pending_revocations(
    session_factory: sessionmaker[Session],
    secret_store: SecretStore,
    provider: BrokerCredentialProvider,
) -> RevocationSweepReport:
    """Retry every `revoke_pending` credential until the broker confirms.

    **One transaction per row**, matching `runner.py`'s rule for its own
    sweeps: a pass that fails halfway must leave the rows it already finished
    written, and one that committed at the end would hold locks across every
    broker round trip.

    A row whose deployment no longer has a usable broker row is left pending
    and counted, not deleted: the credential is still live on whatever broker
    that deployment used to point at, and forgetting about it is precisely the
    outcome `revoke_pending` exists to prevent.
    """
    report = RevocationSweepReport()
    with session_factory() as db:
        pending = [
            (row.id, row.deployment_id, row.aggregator_uuid)
            for row in db.scalars(
                select(BrokerCredential)
                .where(BrokerCredential.state == "revoke_pending")
                .order_by(BrokerCredential.created_at)
            )
        ]

    for row_id, deployment_id, aggregator_uuid in pending:
        try:
            coordinates = coordinates_for(session_factory, secret_store, deployment_id)
        except CredentialError:
            logger.warning(
                "aggregator %s still has a live broker credential and its deployment has no "
                "usable broker row; leaving it revoke_pending",
                aggregator_uuid,
            )
            report.still_pending += 1
            continue
        try:
            with session_factory() as db:
                row = db.get(BrokerCredential, row_id)
                if row is None or row.state != "revoke_pending":
                    continue
                await revoke_credential(db, provider, coordinates, row)
                state = row.state
                db.commit()
            if state == "revoked":
                report.revoked += 1
            else:
                report.still_pending += 1
        except CredentialError:
            # The plugin answered and refused. Logged with a stack trace
            # because it needs an operator, and counted so the sweep's own
            # report says a pass was not clean.
            logger.exception("the broker refused to revoke %s", aggregator_uuid)
            report.failed += 1
    return report


#: How the API and the worker build a provider. A function rather than an
#: import so that E4.6 — and the tests — can substitute one without reaching
#: into either host, and so the dev-stack choice is made in ONE place.
ProviderFactory = Callable[[], BrokerCredentialProvider]


def default_provider() -> BrokerCredentialProvider:
    """`DynsecCredentialProvider`, per fixed choice 4.

    There is no setting selecting the dev provider, and that is deliberate:
    dynsec is required for v1, and a switch that quietly downgraded a
    production deployment to "read the credentials somebody generated for the
    dev stack" is exactly the kind of flag that gets left on.
    `DevBrokerCredentialProvider` is constructed explicitly by whoever wants
    it (E4.6, and the dev-stack tests).

    **Reached through `app.state.credential_provider`, never called directly by
    a route.** Both hosts set that attribute at construction, which is what
    lets a test substitute a provider without a broker — the same shape
    `app.state.mqtt` and `app.state.secret_store` already have.
    """
    return DynsecCredentialProvider()
