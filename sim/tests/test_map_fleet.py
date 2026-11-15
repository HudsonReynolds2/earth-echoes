"""E6.8 SIM acceptance: a reporting fleet, seen through the map's own read.

Phase-6 E6.8: a fleet with coordinates reports, `GET /map/deployments/{id}`
places every Pod and rolls up healthy; `disconnect` on one Aggregator rolls its
Pod and the Deployment up `offline`; `missed_wake_window` takes one Listener
`offline` while its Pod's Aggregator stays online.

Plus E6.9 (D202): the disconnected Aggregator's Listeners read `unreachable`,
because nothing is heard from them any more.

The fleet is the demo hierarchy's Redwood Coast, which the platform fixture
seeds with coordinates at every level (E6.2a): three Aggregators and their
sixteen Listeners, all connected as mock devices. Every assertion reads the
map endpoint over HTTP, exactly as the browser does, after the real consumer
and worker have handled what the devices published. Nothing is written to the
database to make a status.
"""

import pytest
from app.main import API_PREFIX
from app.models import Aggregator, Listener
from conftest import (
    DEP,
    deployment_id_of,
    device_login,
    eventually,
    operator,
    worker_for,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from device import MockAggregator
from scenarios import ScenarioContext, load_scenarios

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SCENARIOS = load_scenarios()

FLEET = ("demo-agg-rc-01", "demo-agg-rc-02", "demo-agg-rc-03")
DOOMED = "demo-agg-rc-02"  # disconnected uncleanly
DREAMER = "demo-agg-rc-03"  # its first Listener misses a wake window


def macs_of(platform, aggregator_uuid: str) -> list[str]:
    with platform.factory() as db:
        return list(
            db.scalars(
                select(Listener.mac)
                .join(Aggregator, Aggregator.id == Listener.aggregator_id)
                .where(Aggregator.aggregator_uuid == aggregator_uuid)
                .order_by(Listener.mac)
            )
        )


async def bring_up(platform, aggregator_uuid: str) -> MockAggregator:
    device = MockAggregator(
        deployment_slug=DEP,
        aggregator_uuid=aggregator_uuid,
        login=device_login(platform, aggregator_uuid),
    )
    for mac in macs_of(platform, aggregator_uuid):
        await device.add_listener(mac)
    await device.connect()
    # A Listener's liveness reaches the platform only in its Aggregator's
    # report (E3.9): each one says it is streaming.
    for listener in device.listeners.values():
        await listener.report()
    return device


async def test_a_reporting_fleet_draws_rolls_up_and_reacts_on_the_map(platform):
    client = operator(platform.app)
    deployment_id = deployment_id_of(platform.factory)
    url = f"{API_PREFIX}/map/deployments/{deployment_id}"

    def map_detail() -> dict:
        response = client.get(url)
        assert response.status_code == 200, response.text
        return response.json()

    def pod_of(detail: dict, aggregator_uuid: str) -> dict:
        return next(pod for pod in detail["pods"] if pod["aggregator_uuid"] == aggregator_uuid)

    with TestClient(platform.app):
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id)
            devices = [await bring_up(platform, agg) for agg in FLEET]
            try:
                # 1. Every Pod is placed, and once all have reported the
                #    Deployment rolls up healthy: no device is anything else.
                def all_healthy():
                    detail = map_detail()
                    return detail if detail["deployment"]["status"] == "healthy" else None

                healthy = await eventually(
                    all_healthy, what="rolled Redwood Coast up healthy", timeout=60.0
                )
                # The fixture also seeds `sim-ghost-01`, an unplaced, silent
                # Aggregator another test deletes to play forgotten hardware
                # (conftest GHOST_AGG). It is no part of this fleet: `unknown`
                # ranks below everything (D60), so it cannot hide the rollup.
                fleet_pods = [p for p in healthy["pods"] if p["aggregator_uuid"] in FLEET]
                assert {pod["aggregator_uuid"] for pod in fleet_pods} == set(FLEET)
                for pod in fleet_pods:
                    assert pod["gps_lat"] is not None and pod["gps_lon"] is not None, pod["name"]
                    assert pod["status"] == "healthy", pod["name"]
                counts = healthy["deployment"]["counts"]
                devices_total = len(FLEET) + len(healthy["listeners"])
                assert counts["healthy"] == devices_total, counts
                others = len(healthy["pods"]) - len(fleet_pods)
                assert counts["unknown"] == others, counts

                # 2. One Aggregator dies uncleanly: its Pod and the Deployment
                #    roll up offline, and its Listeners are unreachable (E6.9).
                doomed = devices[FLEET.index(DOOMED)]
                await SCENARIOS["disconnect"].run(ScenarioContext(aggregator=doomed))

                def doomed_offline():
                    detail = map_detail()
                    pod = pod_of(detail, DOOMED)
                    return detail if pod["aggregator_status"] == "offline" else None

                after = await eventually(
                    doomed_offline, what=f"recorded {DOOMED} offline on the map", timeout=30.0
                )
                cut = pod_of(after, DOOMED)
                assert cut["status"] == "offline"
                assert after["deployment"]["status"] == "offline"
                cut_off = [row for row in after["listeners"] if row["pod_id"] == cut["id"]]
                assert cut_off and {row["status"] for row in cut_off} == {"unreachable"}
                assert pod_of(after, DREAMER)["status"] == "healthy", "the others are untouched"

                # 3. A Listener misses its wake window: it reads offline while
                #    its own Aggregator stays online.
                dreamer = devices[FLEET.index(DREAMER)]
                sleeper = dreamer.first_listener().mac
                await SCENARIOS["missed_wake_window"].run(ScenarioContext(aggregator=dreamer))

                def sleeper_offline():
                    detail = map_detail()
                    row = next(r for r in detail["listeners"] if r["mac"] == sleeper)
                    return detail if row["status"] == "offline" else None

                missed = await eventually(
                    sleeper_offline,
                    what=f"recorded {sleeper} offline on the map",
                    timeout=90.0,
                )
                pod = pod_of(missed, DREAMER)
                assert pod["aggregator_status"] == "healthy", "its Aggregator is still online"
                assert pod["status"] == "offline", "a Pod is the worst of its devices"
            finally:
                for device in devices:
                    if device is not devices[FLEET.index(DOOMED)]:
                        await device.disconnect()
