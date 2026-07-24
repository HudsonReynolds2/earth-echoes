"""E0.1 liveness placeholder.

A bare ASGI callable returning 200 at every path, exactly enough to prove the
container stack runs. The FastAPI application (app factory, /api/v1 prefix,
error envelope, request-ID middleware) is task E0.3 and MUST NOT appear here
before it (phase-0-foundations.md section 4).
"""

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
        return
    body = json.dumps({"status": "ok", "service": "eoe-api-placeholder"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})
