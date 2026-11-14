"""One connection loop per deployment, whatever the startup race (D183).

Found by the E4 walkthrough on a freshly started compose stack. The API and
the worker start before the migrations create `deployment_service`, so the
manager's first `start()` fails and `start_or_retry` schedules a background
retry. That failed `start()` had already marked the manager running, so E5.7b's
30-second `refresh()` started loops for the newly visible deployments, and 7ms
later the retry's own `start()` started them AGAIN. `_begin` overwrote the
task map without checking, orphaning a live loop. The two loops share one
client identifier, so Mosquitto evicted each in turn ("already connected,
closing old connection") about every second, indefinitely, in both processes.

The gate never saw it because every test starts a manager against a database
that is already migrated. This test reproduces the race deterministically, with
no broker: the coordinates point at a closed port, so the loops only retry,
which is all a test of "how many loops exist" needs.
"""

import asyncio
import threading
import uuid

import pytest

from app.controlplane.broker import BrokerCoordinates, MqttClientManager

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _coords(slug: str) -> BrokerCoordinates:
    return BrokerCoordinates(
        deployment_id=uuid.uuid5(uuid.NAMESPACE_DNS, slug),
        slug=slug,
        host="127.0.0.1",
        port=1,  # closed: the loop retries and never connects
        username=f"platform-{slug}",
        password="unused",
        tls_enabled=False,
    )


async def test_a_refresh_during_start_does_not_double_the_loops():
    """The production interleaving, forced: `start()` has marked the manager
    running and is awaiting its database read (in a thread) when a refresh
    tick reads the same rows and begins both loops. Then `start()`'s read
    returns and it begins them again."""
    deployments = [_coords("redwood-coast"), _coords("high-desert")]
    start_may_read = threading.Event()
    calls = {"n": 0}

    def loader() -> list[BrokerCoordinates]:
        calls["n"] += 1
        if calls["n"] == 1:  # start()'s read: held until the refresh has run
            assert start_may_read.wait(timeout=10)
        return deployments

    manager = MqttClientManager(loader)
    starting = asyncio.create_task(manager.start())
    try:
        while calls["n"] == 0:
            await asyncio.sleep(0.01)
        started, _, _ = await manager.refresh()  # lands mid-start
        assert started == 2
        start_may_read.set()
        await asyncio.wait_for(starting, timeout=10)

        live = [t for t in asyncio.all_tasks() if t.get_name().startswith("mqtt-") and not t.done()]
        assert len(live) == len(deployments), (
            f"{len(live)} connection loops for {len(deployments)} deployments: a second loop "
            "with the same client identifier makes the broker evict both in turn"
        )
    finally:
        start_may_read.set()
        await manager.stop()


async def test_beginning_a_deployment_twice_keeps_the_first_loop():
    manager = MqttClientManager(lambda: [])
    await manager.start()
    try:
        coords = _coords("redwood-coast")
        manager._begin(coords)  # noqa: SLF001
        first = manager._tasks[coords.deployment_id]  # noqa: SLF001
        manager._begin(coords)  # noqa: SLF001
        assert manager._tasks[coords.deployment_id] is first  # noqa: SLF001
        assert not first.done()
    finally:
        await manager.stop()
