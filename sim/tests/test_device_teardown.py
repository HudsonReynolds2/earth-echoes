"""A killed device's disconnect error is always retrieved (D172).

The failure this guards against is timing-dependent, so the test does not race
anything: it reproduces the two orderings directly on an aiomqtt-shaped object
and asserts asyncio would not report the error as unretrieved. `_log_traceback`
is the flag asyncio itself checks when it decides to log "Future exception was
never retrieved"; it is True from `set_exception` until someone calls
`exception()` or `result()`.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from aiomqtt import MqttCodeError

from device import _collect_disconnect_error


def _client_with(future: asyncio.Future[None]) -> Any:
    return cast(Any, SimpleNamespace(_disconnected=future))


def test_an_error_already_stored_before_teardown_is_retrieved() -> None:
    async def scenario() -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_exception(MqttCodeError(128, "Unexpected disconnection"))
        _collect_disconnect_error(_client_with(future))
        await asyncio.sleep(0)  # done-callbacks run on the next loop turn
        return future

    future = asyncio.run(scenario())
    assert future._log_traceback is False  # noqa: SLF001


def test_an_error_stored_after_teardown_gave_up_is_retrieved() -> None:
    """The later ordering: the bounded `stack.aclose()` timed out, and paho's
    callback stored the error afterwards."""

    async def scenario() -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        _collect_disconnect_error(_client_with(future))
        future.set_exception(MqttCodeError(128, "Unexpected disconnection"))
        await asyncio.sleep(0)
        return future

    future = asyncio.run(scenario())
    assert future._log_traceback is False  # noqa: SLF001


def test_without_the_collector_asyncio_would_report_it() -> None:
    """The control: the same state with nothing attached IS the bug, so the two
    tests above are not passing vacuously."""

    async def scenario() -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_exception(MqttCodeError(128, "Unexpected disconnection"))
        await asyncio.sleep(0)
        return future

    future = asyncio.run(scenario())
    assert future._log_traceback is True  # noqa: SLF001
    future.exception()  # retrieve it here so this test itself logs nothing
