"""`WS /ws` — live device status and reconciliation updates (task E3.12; spec 13, 9.3).

One socket per browser session. The client names the channels it wants; the
server sends it events from those channels, **filtered to the deployments that
session may see**, and nothing else.

Three properties are load-bearing:

1. **Scoping is applied on the SERVER, per event, per connection.** A socket
   is a long-lived read of everything happening in the platform, so a filter
   done in the browser would be no filter at all — the bytes would already
   have crossed. `visible_deployments(..., VIEW_STATUS)` is evaluated once at
   connect (a session's assignments do not change mid-socket; a change to them
   revokes the session) and every event is checked against it.

2. **Authentication is the session cookie, and there is no CSRF token.** The
   WebSocket handshake is a GET that browsers do not let a page forge headers
   on, and `Sec-WebSocket-Key` plus the same-origin check the CORS layer
   already enforces cover what CSRF covers for state-changing verbs. Nothing
   here changes state: the socket is read-only by construction and grew no
   inbound commands beyond `subscribe`.

3. **The stream is a hint, never a source of truth.** Postgres `NOTIFY` reaches
   whoever is listening at that instant; a browser that reconnects has missed
   whatever happened while it was away, and no amount of buffering here would
   change that honestly. Clients refetch on open and treat events as
   invalidation signals — which is also why this endpoint sends no snapshot.
"""

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.auth.cookies import SESSION_COOKIE, unsign_session_id
from app.auth.rbac import Permission
from app.auth.service import load_valid_session
from app.controlplane.events import Channel, Event
from app.scoping import DeploymentScope, visible_deployments

log = logging.getLogger(__name__)

router = APIRouter()

#: Closed by the handshake if the caller has no session. 1008 is the RFC 6455
#: policy-violation code, which is what an unauthenticated socket is; browsers
#: surface it distinctly from a network failure so the client can stop
#: retrying rather than hammer a login it does not have.
CLOSE_UNAUTHORIZED = 1008


class Subscription:
    """What one connected socket is allowed and has asked to see.

    Kept as an object rather than two locals because the filtering rule is the
    security boundary of this module and belongs somewhere a test can hold it
    directly — see `test_websockets.py`, which drives `wants()` with no socket.
    """

    def __init__(self, scope: DeploymentScope, channels: frozenset[Channel] | None = None) -> None:
        #: Either "all" (an org-wide assignment) or an explicit id set.
        self.scope = scope
        #: None means "everything the scope allows" — the default, so a client
        #: that just connects still receives updates.
        self.channels = channels

    def wants(self, event: Event) -> bool:
        if self.channels is not None and event.channel not in self.channels:
            return False
        if self.scope == "all":
            return True
        return event.deployment_id in self.scope


def _session_scope(websocket: WebSocket, session_id: str | None) -> DeploymentScope | None:
    """Resolve the cookie to a session and its visible deployments.

    A short-lived session of its own: the socket lives for minutes or hours,
    and holding a pooled connection open for all of it would starve the pool
    to do nothing. Everything needed afterwards is a plain set of ids.
    """
    if session_id is None:
        return None
    factory = websocket.app.state.session_factory
    with factory() as db:
        session = load_valid_session(db, session_id)
        if session is None or not session.user.is_active:
            return None
        if not session.user.role_assignments:
            return None
        scope = visible_deployments(session.user.role_assignments, Permission.VIEW_STATUS)
    return scope


@router.websocket("/ws")
async def live_updates(websocket: WebSocket) -> None:
    """Stream events this session may see, until the client goes away."""
    cookie = websocket.cookies.get(SESSION_COOKIE)
    settings = websocket.app.state.settings
    session_id = unsign_session_id(cookie, settings.session_secret) if cookie is not None else None
    scope = _session_scope(websocket, session_id)
    if scope is None:
        # Accept-then-close, so the browser sees a clean policy close rather
        # than an opaque handshake failure it cannot distinguish from the API
        # being down.
        await websocket.accept()
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="authentication required")
        return

    hub = getattr(websocket.app.state, "hub", None)
    if hub is None:
        await websocket.accept()
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="live updates are not enabled")
        return

    await websocket.accept()
    subscription = Subscription(scope)
    queue = hub.subscribe()
    reader = asyncio.create_task(_read_client(websocket, subscription))
    try:
        while True:
            event = await queue.get()
            if not subscription.wants(event):
                continue
            if websocket.client_state is not WebSocketState.CONNECTED:
                return
            await websocket.send_text(event.to_json())
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("a live-update socket failed; closing it")
    finally:
        hub.unsubscribe(queue)
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader


async def _read_client(websocket: WebSocket, subscription: Subscription) -> None:
    """The only inbound message: narrowing the channel set.

    A client may send `{"subscribe": ["device_status"]}` to stop receiving the
    rest. It can only ever NARROW — the scope resolved at connect is not
    reachable from here — so a malicious client gains nothing by lying, which
    is why this loop needs no authorization of its own.
    """
    while True:
        raw = await websocket.receive_text()
        try:
            body = json.loads(raw)
            wanted = body.get("subscribe")
            if isinstance(wanted, list):
                subscription.channels = frozenset(
                    Channel(name) for name in wanted if name in set(Channel)
                )
        except Exception:
            # A client that sends nonsense keeps its socket and its current
            # subscription. Dropping the connection would turn a client bug
            # into a status page that silently stops updating.
            log.debug("ignoring an unreadable websocket message")
