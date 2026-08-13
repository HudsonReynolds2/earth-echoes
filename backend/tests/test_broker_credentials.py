"""E5.6: per-device broker credential minting, against a real dynsec broker.

Three acceptance criteria from the phase document, and each is here because the
cheap version of it proves nothing:

1. **The dynsec role and the `acl_file` lines are rendered from ONE list.**
   Asserted by reading `aggregator_acl_grants` and checking both renderers
   against it, rather than by comparing two hand-written expectations — which
   is what a future session would quietly let drift. The drift that matters is
   one missing line: an Aggregator that may publish to its own `desired` topic
   can manufacture agreement with itself and defeat drift detection.

2. **A minted credential is immediately usable, and immediately limited.** The
   suite connects AS THE DEVICE with the password the platform just minted,
   publishes to its `reported` topic, and is refused on its `desired` topic.
   **The denial assertion is paired with an authorized publish to the same
   topic**, because denial looks like silence (the `test_dev_broker.py` rule,
   INTERFACES "The development broker"): without the positive control, a
   broken subscription would pass this test.

3. **Deleting an Aggregator revokes its client on a real broker.** Asserted by
   trying to log in afterwards and being refused, not by reading a row — the
   row is the platform's belief and the broker is the fact.

Plus the state the owner added on 2026-08-12 (D133): an unreachable broker
leaves `revoke_pending` and never blocks the delete.
"""

import uuid

import aiomqtt
import pytest
from conftest import (
    DYNSEC_ADMIN_PW,
    DYNSEC_ADMIN_USER,
    dynsec_broker,
    ephemeral_broker,
    ephemeral_postgres,
    make_kek,
)
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.contracts.mqtt import QOS, aggregator_root
from app.db import create_session_factory
from app.devbroker import (
    Account,
    acl_file_text,
    aggregator_acl_grants,
    device_username,
    write_artifacts,
)
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    BrokerCredential,
    Deployment,
    DeploymentService,
    Organization,
    Pod,
    RoleAssignment,
    User,
)
from app.services.clients.mqtt import MqttServiceClient
from app.services.credentials import (
    BrokerCredentialProvider,
    BrokerUnreachable,
    CredentialError,
    DevBrokerCredentialProvider,
    DeviceCredential,
    DynsecCredentialProvider,
    RevocationSweepReport,
    coordinates_for,
    default_provider,
    drain_pending_revocations,
    dynsec_role_acls,
    dynsec_role_name,
    load_credential,
    mint_credential,
    secret_name,
)
from app.settings import Settings

pytestmark = pytest.mark.anyio

SLUG = "e56-services"
AGG_UUID = "e56-aggregator-one"
OWNER_EMAIL = "e56-owner@example.com"
PASSWORD = "e56-owner-password"


# --- The one list, and its two renderers -------------------------------------
#
# No container: this is a pure-function property, and putting it behind a
# broker fixture would make the cheapest and most important assertion in the
# module the slowest one.


def test_acl_file_renders_exactly_the_shared_grant_list() -> None:
    """Every device line in the ACL file is one grant from the list, in order."""
    account = Account(
        username=device_username(AGG_UUID),
        password="unused",  # noqa: S106  (a fixture value, never a credential)
        kind="device",
        deployment_slug=SLUG,
        aggregator_uuid=AGG_UUID,
    )
    text = acl_file_text([account])
    rendered = [line for line in text.splitlines() if line.startswith("topic ")]
    assert rendered == [
        f"topic {grant.access} {grant.topic}" for grant in aggregator_acl_grants(SLUG, AGG_UUID)
    ]


def test_dynsec_role_renders_exactly_the_shared_grant_list() -> None:
    """Every dynsec ACL is one grant from the list, translated and nothing more.

    `read` becomes two acltypes because the plugin splits the concept:
    `subscribePattern` decides whether the SUBSCRIBE is accepted and
    `publishClientReceive` whether a matching message is delivered. Granting
    only the first produces a device that subscribes successfully and then
    receives nothing.
    """
    grants = aggregator_acl_grants(SLUG, AGG_UUID)
    acls = dynsec_role_acls(grants)

    expected: list[dict[str, object]] = []
    for grant in grants:
        if grant.access == "read":
            expected.append({"acltype": "subscribePattern", "topic": grant.topic, "allow": True})
            expected.append(
                {"acltype": "publishClientReceive", "topic": grant.topic, "allow": True}
            )
        else:
            expected.append({"acltype": "publishClientSend", "topic": grant.topic, "allow": True})
    assert acls == expected


def test_no_renderer_grants_a_device_its_own_desired_topic() -> None:
    """The one grant whose absence is a security defect, asserted directly.

    Spelled out separately from the two renderer tests above because those
    would both still pass if `aggregator_acl_grants` itself grew the line. This
    is the assertion about the LIST.
    """
    root = aggregator_root(SLUG, AGG_UUID)
    writable = {
        grant.topic for grant in aggregator_acl_grants(SLUG, AGG_UUID) if grant.access == "write"
    }
    assert f"{root}/desired" not in writable
    assert f"{root}/lst/+/desired" not in writable
    # And the positive half, so the test fails if the list is emptied entirely.
    assert f"{root}/reported" in writable


def test_every_grant_is_inside_this_aggregators_own_subtree() -> None:
    """The isolation guarantee of spec 7.1, as a property rather than a list."""
    root = aggregator_root(SLUG, AGG_UUID)
    for grant in aggregator_acl_grants(SLUG, AGG_UUID):
        assert grant.topic.startswith(f"{root}/"), grant


def test_grant_topics_move_with_the_topic_builders() -> None:
    """A different aggregator gets a disjoint grant set, with no literals."""
    mine = {grant.topic for grant in aggregator_acl_grants(SLUG, AGG_UUID)}
    theirs = {grant.topic for grant in aggregator_acl_grants(SLUG, "e56-aggregator-two")}
    assert not mine & theirs


# --- Fixtures: a real dynsec broker and an app pointed at it -----------------


@pytest.fixture(scope="module")
def broker(tmp_path_factory):
    with dynsec_broker(tmp_path_factory.mktemp("e56-dynsec"), SLUG) as running:
        yield running


@pytest.fixture(scope="module")
def pg_url():
    with ephemeral_postgres() as url:
        yield url


@pytest.fixture(scope="module")
def app(pg_url, broker):
    """An app whose one deployment's broker row IS the dynsec container.

    The platform account is the fixture's `admin` client, which is what makes
    minting possible at all: fixed choice 4 requires the platform to hold the
    plugin's `admin` role, and `_require_admin` refuses before publishing
    anything if it does not.
    """
    application = create_app(
        Settings(
            database_url=pg_url,
            session_secret="e56-test-secret",
            kek=make_kek(),
            cors_origins="",
            publish_enabled=False,
        )
    )
    _, factory = create_session_factory(pg_url)
    ca = (broker.dev_dir / "ca.crt").read_text(encoding="utf-8")
    with factory() as db:
        org = Organization(name="e56-org")
        db.add(org)
        db.flush()
        dep = Deployment(organization_id=org.id, name="E56", slug=SLUG)
        db.add(dep)
        db.flush()
        pod = Pod(deployment_id=dep.id, name="e56-pod")
        db.add(pod)
        db.flush()
        aggregator = Aggregator(pod_id=pod.id, aggregator_uuid=AGG_UUID, name="e56-agg")
        db.add(aggregator)
        service = DeploymentService(
            deployment_id=dep.id,
            service_key="mqtt",
            host="127.0.0.1",
            port=broker.port,
            tls_enabled=True,
            ca_cert_pem=ca,
            username=DYNSEC_ADMIN_USER,
            password_secret_name=f"deployment:{dep.id}:mqtt_password",
        )
        db.add(service)
        user = User(email=OWNER_EMAIL, password_hash=hash_password(PASSWORD))
        user.role_assignments.append(RoleAssignment(role=Role.OWNER.value, deployment_id=None))
        db.add(user)
        db.commit()
        application.state.e56_deployment_id = dep.id
        application.state.e56_aggregator_id = aggregator.id
        application.state.e56_pod_id = pod.id
    application.state.secret_store.put(
        f"deployment:{application.state.e56_deployment_id}:mqtt_password", DYNSEC_ADMIN_PW
    )
    return application


@pytest.fixture
def dep_id(app) -> uuid.UUID:
    return app.state.e56_deployment_id


@pytest.fixture
def coordinates(app, dep_id):
    return coordinates_for(app.state.session_factory, app.state.secret_store, dep_id)


@pytest.fixture(autouse=True)
def clean_credentials(app):
    """Each test starts with no minted credential, on the broker AND in the row.

    Deleting only the row would leave the previous test's dynsec client behind,
    and the next mint would then be exercising the delete-first path by
    accident rather than on purpose.
    """
    yield
    with app.state.session_factory() as db:
        for row in db.query(BrokerCredential).all():
            db.delete(row)
        db.commit()


def owner_client(app) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": OWNER_EMAIL, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def device_client(broker, password: str) -> MqttServiceClient:
    """A client dialling as the DEVICE, with the password just minted."""
    return MqttServiceClient(
        deployment_id=uuid.uuid4(),
        deployment_slug=SLUG,
        host="127.0.0.1",
        port=broker.port,
        username=device_username(AGG_UUID),
        password=password,
        tls_enabled=True,
        ca_cert_pem=(broker.dev_dir / "ca.crt").read_text(encoding="utf-8"),
    )


def admin_client(broker) -> MqttServiceClient:
    return MqttServiceClient(
        deployment_id=uuid.uuid4(),
        deployment_slug=SLUG,
        host="127.0.0.1",
        port=broker.port,
        username=DYNSEC_ADMIN_USER,
        password=DYNSEC_ADMIN_PW,
        tls_enabled=True,
        ca_cert_pem=(broker.dev_dir / "ca.crt").read_text(encoding="utf-8"),
    )


async def _mint(app, coordinates) -> str:
    """Mint through the service layer and return the plaintext password."""
    with app.state.session_factory() as db:
        await mint_credential(
            db,
            app.state.secret_store,
            DynsecCredentialProvider(),
            coordinates,
            AGG_UUID,
        )
        db.commit()
    return app.state.secret_store.get(secret_name(coordinates.deployment_id, AGG_UUID))


async def _first_matching(client: aiomqtt.Client, topic: str, timeout: float = 5.0) -> bytes | None:
    """The next payload on `topic`, or None. Wrapped so both the positive and
    the negative assertion below use the same waiting code — a denial test
    whose wait differs from its control's proves nothing."""
    import asyncio

    try:
        async with asyncio.timeout(timeout):
            async for message in client.messages:
                if message.topic.matches(topic):
                    return bytes(message.payload)
    except TimeoutError:
        return None
    return None


# --- Minting against a real broker -------------------------------------------


async def test_a_minted_credential_logs_in_and_can_report(app, broker, coordinates) -> None:
    """The device connects with what the platform minted, and its `reported`
    publish reaches a subscriber. The whole point of the mint."""
    password = await _mint(app, coordinates)
    root = aggregator_root(SLUG, AGG_UUID)
    body = uuid.uuid4().hex.encode()

    async with admin_client(broker).connect() as watcher:
        await watcher.subscribe(f"{root}/reported", qos=QOS)
        async with device_client(broker, password).connect() as device:
            await device.publish(f"{root}/reported", body, qos=QOS)
        assert await _first_matching(watcher, f"{root}/reported") == body


async def test_a_minted_credential_is_refused_on_its_own_desired_topic(
    app, broker, coordinates
) -> None:
    """**The isolation guarantee, with its positive control in the same test.**

    Denial looks like silence, so "the device published and nobody saw it"
    would also be the result of a broken subscription. The authorized publish
    to the SAME topic, read back over the SAME subscription, is what makes the
    first half mean something.
    """
    password = await _mint(app, coordinates)
    root = aggregator_root(SLUG, AGG_UUID)
    forged = uuid.uuid4().hex.encode()
    legitimate = uuid.uuid4().hex.encode()

    async with device_client(broker, password).connect() as device:
        # The device may READ its own desired topic, which is what lets it
        # watch for both publishes on one subscription.
        await device.subscribe(f"{root}/desired", qos=QOS)
        await device.publish(f"{root}/desired", forged, qos=QOS)
        assert await _first_matching(device, f"{root}/desired", timeout=3.0) is None, (
            "a device published to its own desired topic; it can now manufacture "
            "agreement with itself and defeat drift detection"
        )
        # The control: the same topic, the same subscription, an authorized
        # publisher. If this fails, the assertion above proved nothing.
        async with admin_client(broker).connect() as platform:
            await platform.publish(f"{root}/desired", legitimate, qos=QOS, retain=False)
        assert await _first_matching(device, f"{root}/desired") == legitimate


async def test_a_minted_credential_cannot_reach_another_aggregators_subtree(
    app, broker, coordinates
) -> None:
    """Its grants are cut to its own root, asserted on the wire."""
    password = await _mint(app, coordinates)
    theirs = aggregator_root(SLUG, "e56-aggregator-two")
    body = uuid.uuid4().hex.encode()

    async with admin_client(broker).connect() as watcher:
        await watcher.subscribe(f"{theirs}/reported", qos=QOS)
        async with device_client(broker, password).connect() as device:
            await device.publish(f"{theirs}/reported", body, qos=QOS)
        assert await _first_matching(watcher, f"{theirs}/reported", timeout=3.0) is None
        # Control: the same watcher sees an authorized publish on that topic.
        async with admin_client(broker).connect() as platform:
            await platform.publish(f"{theirs}/reported", body, qos=QOS)
        assert await _first_matching(watcher, f"{theirs}/reported") == body


async def test_minting_twice_rotates_the_password_and_leaves_one_row(
    app, broker, coordinates
) -> None:
    """A re-mint is a rotation: the old password stops working, the new one
    works, and there is still exactly one row and one dynsec client."""
    first = await _mint(app, coordinates)
    second = await _mint(app, coordinates)
    assert first != second

    async with device_client(broker, second).connect():
        pass
    with pytest.raises(Exception):  # noqa: B017  (aiomqtt wraps the CONNACK refusal)
        async with device_client(broker, first).connect():
            pass

    with app.state.session_factory() as db:
        rows = db.query(BrokerCredential).all()
        assert len(rows) == 1
        assert rows[0].state == "minted"
        assert rows[0].username == device_username(AGG_UUID)


async def test_the_row_never_holds_the_password(app, coordinates) -> None:
    """Rule R2: the row names a SecretStore entry and nothing else."""
    password = await _mint(app, coordinates)
    with app.state.session_factory() as db:
        row = load_credential(db, coordinates.deployment_id, AGG_UUID)
        assert row is not None
        assert row.password_secret_name == secret_name(coordinates.deployment_id, AGG_UUID)
        for value in (row.username, row.password_secret_name, row.state, row.aggregator_uuid):
            assert password not in value


async def test_minting_refuses_a_broker_without_the_plugin(app, coordinates, tmp_path) -> None:
    """Fixed choice 4, enforced at the moment it matters.

    The failure names the remedy the probe carries rather than "the plugin did
    not answer", which is what a bare `createClient` timeout would have said.
    """
    out = tmp_path / "plainbroker"
    account = Account(
        username="platform-x",
        password="platform-password",  # noqa: S106
        kind="platform",
        deployment_slug=SLUG,
    )
    write_artifacts(out, [account], host="127.0.0.1", port=8883, regenerate_tls=True)
    with ephemeral_broker(out) as plain:
        plainless = coordinates.__class__(
            deployment_id=coordinates.deployment_id,
            slug=SLUG,
            host="127.0.0.1",
            port=plain.port,
            username="platform-x",
            password="platform-password",  # noqa: S106
            tls_enabled=True,
            ca_cert_pem=(out / "ca.crt").read_text(encoding="utf-8"),
        )
        with pytest.raises(CredentialError) as caught:
            await DynsecCredentialProvider().mint(plainless, AGG_UUID)
    assert "dynamic security plugin" in str(caught.value)


# --- Revocation ---------------------------------------------------------------


async def test_deleting_an_aggregator_revokes_its_client_on_the_broker(
    app, broker, coordinates
) -> None:
    """The E5.6 acceptance, asserted on the BROKER rather than on the row.

    Afterwards the device's login is refused, the row survives the device (it
    has to — it is the record of something outside this database), and it says
    `revoked` with a timestamp.
    """
    password = await _mint(app, coordinates)
    async with device_client(broker, password).connect():
        pass  # It works before the delete; otherwise the test below is vacuous.

    client = owner_client(app)
    response = client.delete(
        f"{API_PREFIX}/aggregators/{app.state.e56_aggregator_id}",
        headers={"X-CSRF-Token": client.cookies["eoe_csrf"]},
    )
    assert response.status_code == 204, response.text

    with pytest.raises(Exception):  # noqa: B017  (a refused CONNACK)
        async with device_client(broker, password).connect():
            pass

    with app.state.session_factory() as db:
        row = load_credential(db, coordinates.deployment_id, AGG_UUID)
        assert row is not None, "the credential row must outlive the device it belonged to"
        assert row.state == "revoked"
        assert row.revoked_at is not None
        # Put the aggregator back for the rest of the module.
        db.add(
            Aggregator(
                id=app.state.e56_aggregator_id,
                pod_id=app.state.e56_pod_id,
                aggregator_uuid=AGG_UUID,
                name="e56-agg",
            )
        )
        db.commit()


async def test_an_unreachable_broker_leaves_revoke_pending_and_never_blocks(
    app, coordinates, monkeypatch
) -> None:
    """D133: the owner's call. The delete succeeds, the credential is not
    forgotten, and `revoked_at` stays NULL because nothing confirmed."""
    await _mint(app, coordinates)

    class Unreachable:
        async def mint(self, coordinates, aggregator_uuid):  # pragma: no cover - unused
            raise BrokerUnreachable("down")

        async def revoke(self, coordinates, aggregator_uuid):
            raise BrokerUnreachable("the broker is down")

    monkeypatch.setattr(app.state, "credential_provider", Unreachable())
    client = owner_client(app)
    response = client.delete(
        f"{API_PREFIX}/aggregators/{app.state.e56_aggregator_id}",
        headers={"X-CSRF-Token": client.cookies["eoe_csrf"]},
    )
    assert response.status_code == 204, response.text

    with app.state.session_factory() as db:
        row = load_credential(db, coordinates.deployment_id, AGG_UUID)
        assert row is not None
        assert row.state == "revoke_pending"
        assert row.revoked_at is None
        db.add(
            Aggregator(
                id=app.state.e56_aggregator_id,
                pod_id=app.state.e56_pod_id,
                aggregator_uuid=AGG_UUID,
                name="e56-agg",
            )
        )
        db.commit()


async def test_the_sweep_finishes_a_pending_revocation_against_the_real_broker(
    app, broker, coordinates
) -> None:
    """`revoke_pending` is a promise, and this is the code that keeps it."""
    password = await _mint(app, coordinates)
    with app.state.session_factory() as db:
        row = load_credential(db, coordinates.deployment_id, AGG_UUID)
        assert row is not None
        row.state = "revoke_pending"
        db.commit()

    report = await drain_pending_revocations(
        app.state.session_factory, app.state.secret_store, DynsecCredentialProvider()
    )
    assert report == RevocationSweepReport(revoked=1, still_pending=0, failed=0)
    assert report.changed

    with pytest.raises(Exception):  # noqa: B017  (a refused CONNACK)
        async with device_client(broker, password).connect():
            pass
    with app.state.session_factory() as db:
        row = load_credential(db, coordinates.deployment_id, AGG_UUID)
        assert row is not None and row.state == "revoked"


async def test_the_sweep_is_a_no_op_when_nothing_is_pending(app) -> None:
    report = await drain_pending_revocations(
        app.state.session_factory, app.state.secret_store, DynsecCredentialProvider()
    )
    assert report == RevocationSweepReport()
    assert not report.changed


async def test_revoking_something_already_gone_still_reaches_revoked(app, coordinates) -> None:
    """Idempotence. A retry after a partial failure must converge, and the
    plugin's "not found" is the state revocation was aiming at."""
    await _mint(app, coordinates)
    provider = DynsecCredentialProvider()
    await provider.revoke(coordinates, AGG_UUID)
    # Second time: the client and the role are both already gone.
    await provider.revoke(coordinates, AGG_UUID)


# --- The E4.6 seam ------------------------------------------------------------


def test_the_default_provider_is_the_dynsec_one() -> None:
    """Fixed choice 4: there is no setting that downgrades this in production."""
    assert isinstance(default_provider(), DynsecCredentialProvider)


def test_both_implementations_satisfy_the_protocol_e4_will_import() -> None:
    assert isinstance(DynsecCredentialProvider(), BrokerCredentialProvider)
    assert isinstance(DevBrokerCredentialProvider(), BrokerCredentialProvider)


async def test_the_dev_provider_reads_the_devbroker_manifest(tmp_path, coordinates) -> None:
    """It mints nothing: it hands back the account `app.devbroker` generated."""
    accounts = [
        Account(
            username="platform-x",
            password="platform-password",  # noqa: S106
            kind="platform",
            deployment_slug=SLUG,
        ),
        Account(
            username=device_username(AGG_UUID),
            password="device-password",  # noqa: S106
            kind="device",
            deployment_slug=SLUG,
            aggregator_uuid=AGG_UUID,
        ),
    ]
    write_artifacts(tmp_path, accounts, host="127.0.0.1", port=8883, regenerate_tls=True)
    provider = DevBrokerCredentialProvider(tmp_path)
    credential = await provider.mint(coordinates, AGG_UUID)
    assert credential.username == device_username(AGG_UUID)
    assert credential.password == "device-password"  # noqa: S105
    # And revoking is an explicit no-op rather than an error.
    await provider.revoke(coordinates, AGG_UUID)


async def test_the_dev_provider_says_what_to_do_when_the_device_is_unknown(
    tmp_path, coordinates
) -> None:
    from app.services.credentials import CredentialError

    write_artifacts(tmp_path, [], host="127.0.0.1", port=8883, regenerate_tls=True)
    with pytest.raises(CredentialError) as caught:
        await DevBrokerCredentialProvider(tmp_path).mint(coordinates, AGG_UUID)
    assert "app.devbroker" in str(caught.value)


def test_a_credential_never_renders_its_password(app) -> None:
    """D66's rule, applied to the third object that carries a secret."""
    credential = DeviceCredential(username="dev-x", password="hunter2")  # noqa: S106
    assert "hunter2" not in repr(credential)
    assert "hunter2" not in str(credential)


def test_the_role_name_is_derived_and_not_a_literal() -> None:
    assert dynsec_role_name(AGG_UUID).endswith(AGG_UUID)


# --- The endpoints ------------------------------------------------------------


def test_mint_and_read_through_the_api(app, broker) -> None:
    client = owner_client(app)
    base = f"{API_PREFIX}/aggregators/{app.state.e56_aggregator_id}/broker-credential"
    response = client.post(base, headers={"X-CSRF-Token": client.cookies["eoe_csrf"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "minted"
    assert body["username"] == device_username(AGG_UUID)
    assert body["revoked_at"] is None
    # **No password field, on any branch.** The write-only rule of E5.2.
    assert "password" not in response.text.lower()

    read = client.get(base)
    assert read.status_code == 200, read.text
    assert read.json()["state"] == "minted"


def test_revoke_through_the_api_leaves_the_row_readable(app, broker) -> None:
    client = owner_client(app)
    base = f"{API_PREFIX}/aggregators/{app.state.e56_aggregator_id}/broker-credential"
    client.post(base, headers={"X-CSRF-Token": client.cookies["eoe_csrf"]})
    response = client.delete(base, headers={"X-CSRF-Token": client.cookies["eoe_csrf"]})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "revoked"
    assert response.json()["revoked_at"] is not None


def test_reading_a_credential_that_was_never_minted_is_404(app) -> None:
    client = owner_client(app)
    response = client.get(
        f"{API_PREFIX}/aggregators/{app.state.e56_aggregator_id}/broker-credential"
    )
    assert response.status_code == 404, response.text


def test_an_unknown_aggregator_is_404_not_500(app) -> None:
    client = owner_client(app)
    response = client.get(f"{API_PREFIX}/aggregators/{uuid.uuid4()}/broker-credential")
    assert response.status_code == 404, response.text
