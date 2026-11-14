"""E6.6: the map's live path starts with an event the map's subscription can see.

Phase-6 E6.6's backend acceptance: the `disconnect` scenario's LWT event
carries the `deployment_id` the map's subscription filter needs. The browser
only refetches the map on events its socket delivers, and `/ws` delivers an
event only to sessions whose `Subscription` wants that `deployment_id` (E3.12).
An event without the right Deployment would leave a scoped operator's map
stale, silently.

Driven the way SIM's `disconnect` scenario reaches the platform: a device that
said `online`, then its broker-held will, `offline`, stamped at CONNECT time and
so OLDER than the heartbeat before it (E3.8). The real consumer handles both
and publishes through the real `publish`, which is wrapped only to record what
it was given.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ephemeral_postgres
from sqlalchemy import delete, select

import app.controlplane.consumer as consumer_module
from app.api.ws import Subscription
from app.contracts.mqtt import StatusMessage, encode, status_topic
from app.controlplane.broker import InboundMessage
from app.controlplane.consumer import ReportedConsumer, ReportOutcome
from app.controlplane.events import Channel, Event
from app.db import create_session_factory
from app.map.service import deployment_detail
from app.models import Aggregator, AggregatorStatus, Deployment, Pod
from app.seed import seed_demo_hierarchy

pytestmark = [pytest.mark.integration]

RC = "redwood-coast"
HD = "high-desert"
AGG = "demo-agg-rc-02"


@pytest.fixture(scope="module")
def factory():
    with ephemeral_postgres() as url:
        _, sessions = create_session_factory(url)
        with sessions() as db:
            seed_demo_hierarchy(db)
            db.commit()
        yield sessions


@pytest.fixture
def published(factory, monkeypatch) -> list[Event]:
    seen: list[Event] = []
    real = consumer_module.publish

    def recording(db, event: Event) -> None:
        seen.append(event)
        real(db, event)

    monkeypatch.setattr(consumer_module, "publish", recording)
    yield seen
    with factory() as db:
        db.execute(delete(AggregatorStatus))
        db.commit()


def deployment_id(factory, slug: str) -> uuid.UUID:
    with factory() as db:
        return db.scalars(select(Deployment.id).where(Deployment.slug == slug)).one()


def status(factory, state: str, at: datetime) -> InboundMessage:
    return InboundMessage(
        deployment_id=deployment_id(factory, RC),
        deployment_slug=RC,
        topic=status_topic(RC, AGG),
        payload=encode(StatusMessage(state=state, at=at)),
        qos=1,
        retain=True,
    )


def test_the_disconnect_lwt_reaches_exactly_its_deployments_map(factory, published):
    consumer = ReportedConsumer(factory)
    connected = datetime.now(UTC) - timedelta(minutes=5)
    assert consumer.consume(status(factory, "online", datetime.now(UTC))) is ReportOutcome.ONLINE
    # The will: the bytes the broker held since CONNECT, so its `at` is older.
    assert consumer.consume(status(factory, "offline", connected)) is ReportOutcome.OFFLINE

    events = [event for event in published if event.channel is Channel.DEVICE_STATUS]
    assert [event.data["online"] for event in events] == [True, False]
    lwt = events[-1]

    redwood = deployment_id(factory, RC)
    with factory() as db:
        aggregator = db.scalars(select(Aggregator).where(Aggregator.aggregator_uuid == AGG)).one()
        assert db.get_one(Pod, aggregator.pod_id).deployment_id == redwood
    assert lwt.deployment_id == redwood
    assert (lwt.entity_type, lwt.entity_id) == ("aggregator", str(aggregator.id))

    # The filter `/ws` applies per session (E3.12): the Redwood operator's
    # socket carries it, the High Desert operator's does not, and an org-wide
    # session sees it.
    assert Subscription({redwood}).wants(lwt) is True
    assert Subscription({deployment_id(factory, HD)}).wants(lwt) is False
    assert Subscription("all").wants(lwt) is True
    # It survives the wire unchanged, which is what the browser reads.
    assert Event.from_json(lwt.to_json()).deployment_id == redwood

    # And the refetch it triggers reads the new state: the Pod is offline.
    with factory() as db:
        detail = deployment_detail(db, db.get_one(Deployment, redwood))
    ridge = next(pod for pod in detail.pods if pod.aggregator_uuid == AGG)
    assert ridge.aggregator_status == "offline" and ridge.status == "offline"
    assert detail.deployment.status == "offline"
