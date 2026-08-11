"""The spec 9.3 liveness verdict, in one place (task E3.9).

Spec 9.3 splits the online/offline question by device kind, and the two
halves have different sources:

* **Aggregators** answer through MQTT LWT — `aggregator_status.online`, E3.8.
* **Listeners** hold no MQTT session at all (spec 6.4), so their verdict is
  derived from the Aggregator-tracked liveness state of spec 6.5:
  `streaming` and `sleeping` BOTH display as healthy, and only `offline`
  displays as offline.

That `sleeping` counts as healthy is the whole point of spec 6.5 and the
easiest thing in this system to get wrong. A duty-cycled Listener is silent
for most of its life by design; a platform that painted silence as failure
would report a correctly-working deployment as a fleet-wide outage every
night. The Listener declares its own wake time over the local link, the
Aggregator trusts that declaration and decides when it has been missed, and
this module only reads the answer.

It lives here, alone and pure, because E3.11 (timeline), E3.12 (websocket)
and E6 (map colour) all need the same rule. Three copies of a two-line
function is three chances for one of them to decide that a sleeping Listener
looks broken.
"""

from typing import Literal

#: The spec 6.5 vocabulary, in the order a Listener moves through it.
LIVENESS_STATES: tuple[str, ...] = ("streaming", "sleeping", "offline")

#: The states spec 9.3 displays as healthy. `sleeping` is here on purpose.
HEALTHY_LIVENESS: frozenset[str] = frozenset({"streaming", "sleeping"})

Verdict = Literal["healthy", "offline", "unknown"]


def listener_verdict(liveness_state: str | None) -> Verdict:
    """Spec 9.3's displayed status for one Listener.

    `None` is `unknown`, NOT offline: a Listener nobody has reported on yet
    has not been observed to be anything, and painting it as failed would
    accuse a device that may not even be powered on yet of being broken. E1
    lets an operator enter a Listener into inventory long before it ships.
    """
    if liveness_state is None:
        return "unknown"
    if liveness_state in HEALTHY_LIVENESS:
        return "healthy"
    return "offline"
