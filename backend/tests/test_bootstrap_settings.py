"""E4.6: the Aggregator's `settings.yaml` and its bootstrap block (spec 16.4,
16.5; phase-4 E4.6; D162, D167, D176).

Acceptance:

1. **Both flag states are tested.** With credentials off, the file is still
   valid YAML that names what is missing.
2. **The embedded `aggregator_uuid` equals the inventory row's**, is carried
   on the record, and is never regenerated.
3. **A deployment that is not verified, or has no MQTT row, is refused**,
   naming the deployment.

Plus the property that makes regeneration safe: an Aggregator that already
holds a live broker login keeps it rather than being re-minted, which would
break the card already in the Pi.
"""

import uuid

import pytest
import yaml
from conftest import ephemeral_postgres, make_kek
from sqlalchemy import select, update

from app.db import create_session_factory
from app.devbroker import plan_accounts, register_services
from app.models import Aggregator, Deployment, Pod
from app.provisioning.bootstrap import (
    CREDENTIAL_FIELDS,
    BootstrapRefused,
    generate_settings_yaml,
    settings_yaml_path,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy
from app.services.credentials import DeviceCredential, load_credential
from app.settings import Settings

pytestmark = pytest.mark.integration

FAKE_CA_PEM = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"


class FakeProvider:
    """A provider with no broker behind it: counts mints and hands back a
    distinct password each time, so a re-mint would be visible."""

    def __init__(self) -> None:
        self.minted: list[str] = []

    async def mint(self, coordinates, aggregator_uuid: str) -> DeviceCredential:
        self.minted.append(aggregator_uuid)
        return DeviceCredential(
            username=f"agg-{aggregator_uuid}",
            password=f"e46-minted-password-{len(self.minted)}-{uuid.uuid4().hex[:6]}",
        )

    async def revoke(self, coordinates, aggregator_uuid: str) -> None:  # pragma: no cover
        raise AssertionError("E4.6 never revokes")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def env():
    """Demo hierarchy, a broker row per deployment, and every deployment
    verified except where a test says otherwise."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        store = SecretStore(factory, make_kek())
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
            register_services(
                db, store, plan_accounts(db), host="broker.example", port=8883,
                ca_cert_pem=FAKE_CA_PEM,
            )  # fmt: skip
            db.execute(update(Deployment).values(services_status="verified"))
            db.commit()
        yield factory, store


def _pods(db) -> list[Pod]:
    return list(
        db.scalars(select(Pod).join(Aggregator, Aggregator.pod_id == Pod.id).order_by(Pod.name))
    )


async def _generate(factory, store, provider, pod_id, *, credentials: bool):
    with factory() as db:
        generated = await generate_settings_yaml(
            db, factory, store, provider, pod_id, include_credentials=credentials
        )
        db.commit()
        return generated


# --- 1. Both flag states --------------------------------------------------------


@pytest.mark.anyio
async def test_credentials_on_embeds_the_devices_own_login(env):
    factory, store = env
    provider = FakeProvider()
    with factory() as db:
        pod = _pods(db)[0]
        aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod.id))
    generated = await _generate(factory, store, provider, pod.id, credentials=True)
    document = yaml.safe_load(generated.content)

    assert generated.path == settings_yaml_path(pod.id)
    assert document["aggregator_uuid"] == aggregator.aggregator_uuid
    assert document["mqtt"]["host"] == "broker.example"
    assert document["mqtt"]["port"] == 8883
    assert document["mqtt"]["tls"] is True
    assert document["mqtt"]["ca_cert"] == FAKE_CA_PEM
    # A literal block, so the PEM reads as a PEM to whoever opens the file on
    # the Pi. Found by the manual check: the default folded style was valid
    # YAML but unrecognisable.
    assert b"  ca_cert: |\n    -----BEGIN CERTIFICATE-----\n    ZmFrZQ==\n" in generated.content
    with factory() as db:
        row = load_credential(db, pod.deployment_id, aggregator.aggregator_uuid)
        assert document["mqtt"]["username"] == row.username
        assert document["mqtt"]["password"] == store.get(row.password_secret_name)
    assert "missing" not in document
    # Plaintext by design (spec 16.4), and the file says so.
    assert b"PLAINTEXT BY DESIGN" in generated.content


@pytest.mark.anyio
async def test_credentials_off_is_valid_yaml_naming_what_is_missing(env):
    factory, store = env
    provider = FakeProvider()
    with factory() as db:
        pod = _pods(db)[1]
    generated = await _generate(factory, store, provider, pod.id, credentials=False)
    document = yaml.safe_load(generated.content)

    assert "username" not in document["mqtt"]
    assert "password" not in document["mqtt"]
    assert document["missing"] == list(CREDENTIAL_FIELDS)
    assert provider.minted == [], "the flag off must not touch the broker"


def test_the_flag_defaults_on():
    settings = Settings(database_url="postgresql+psycopg://x/y", session_secret="s", kek=make_kek())
    assert settings.bootstrap_credentials is True


# --- 2. Identity and the record -----------------------------------------------------


@pytest.mark.anyio
async def test_the_record_carries_the_inventory_uuid_and_the_platform_id(env):
    factory, store = env
    with factory() as db:
        pod = _pods(db)[2]
        aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod.id))
        before = aggregator.aggregator_uuid
    generated = await _generate(factory, store, FakeProvider(), pod.id, credentials=True)
    (record,) = generated.records

    assert record.target_type == "aggregator"
    # D75: revisions and device_state key an Aggregator by its row id.
    assert record.target_id == str(aggregator.id)
    assert record.aggregator_uuid == before
    assert record.file_kind == "settings_yaml"
    assert record.file_path == generated.path
    with factory() as db:
        assert db.get(Aggregator, aggregator.id).aggregator_uuid == before, "never regenerated"


@pytest.mark.anyio
async def test_regeneration_reuses_a_live_login_instead_of_reminting(env):
    """Re-minting would change the password on the broker and strand the card
    already in the Pi."""
    factory, store = env
    provider = FakeProvider()
    with factory() as db:
        pod = _pods(db)[3]
    first = yaml.safe_load(
        (await _generate(factory, store, provider, pod.id, credentials=True)).content
    )
    second = yaml.safe_load(
        (await _generate(factory, store, provider, pod.id, credentials=True)).content
    )
    assert len(provider.minted) == 1
    assert first["mqtt"]["password"] == second["mqtt"]["password"]


# --- 3. The spec 16.5 refusal ---------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["unconfigured", "pending_verification", "degraded"])
async def test_an_unverified_deployment_is_refused_naming_it(env, status):
    factory, store = env
    provider = FakeProvider()
    with factory() as db:
        pod = _pods(db)[0]
        deployment = db.get(Deployment, pod.deployment_id)
        name = deployment.name
        deployment.services_status = status
        db.commit()
    try:
        with pytest.raises(BootstrapRefused) as refused:
            await _generate(factory, store, provider, pod.id, credentials=True)
        assert refused.value.deployment_name == name
        assert name in str(refused.value)
        assert status in str(refused.value)
        assert provider.minted == [], "nothing is minted for a refused bundle"
    finally:
        with factory() as db:
            db.get(Deployment, pod.deployment_id).services_status = "verified"
            db.commit()


@pytest.mark.anyio
async def test_a_deployment_with_no_mqtt_row_is_refused_naming_it(env):
    """A verified status with no broker row behind it (the database forbids a
    half-filled `mqtt` row, so absence is the case that can actually occur)."""
    factory, store = env
    with factory() as db:
        organization_id = db.scalars(select(Deployment.organization_id)).first()
        deployment = Deployment(
            organization_id=organization_id,
            name="E4.6 brokerless",
            slug=f"e46-brokerless-{uuid.uuid4().hex[:6]}",
            services_status="verified",
        )
        db.add(deployment)
        db.flush()
        pod = Pod(deployment_id=deployment.id, name="E4.6 brokerless pod")
        db.add(pod)
        db.flush()
        db.add(Aggregator(pod_id=pod.id, aggregator_uuid=f"e46-{uuid.uuid4().hex[:8]}"))
        db.commit()
        pod_id = pod.id
    provider = FakeProvider()
    with pytest.raises(BootstrapRefused) as refused:
        await _generate(factory, store, provider, pod_id, credentials=True)
    assert refused.value.deployment_name == "E4.6 brokerless"
    assert "E4.6 brokerless" in str(refused.value)
    assert "no usable MQTT broker row" in str(refused.value)
    assert provider.minted == []
