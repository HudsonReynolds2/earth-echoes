"""The MQTT control-plane contract (tasks E3.1, E3.3; spec 7.2 and 7.3).

**PUBLISHED INTERFACE — the simulation harness (SIM epic) imports this module
and device firmware is written against it.** Additive change only: renaming a
topic builder or a payload field breaks devices in the field, and a snapshot
whose bytes change breaks the E2 checksum contract (D52).

Topic namespace (spec 7.2), all under a deployment-scoped root, where `{dep}`
is the Deployment SLUG (never the name, never the UUID — D36), `{agg}` the
Aggregator's `aggregator_uuid`, and `{mac}` a Listener MAC in the normalized
uppercase colon-separated form:

| Topic                                       | Direction | Retained |
|---------------------------------------------|-----------|----------|
| `eoe/{dep}/agg/{agg}/desired`                 | to device | yes |
| `eoe/{dep}/agg/{agg}/reported`                | to platform | no |
| `eoe/{dep}/agg/{agg}/status`                  | to platform (LWT) | yes |
| `eoe/{dep}/agg/{agg}/event`                   | to platform | no |
| `eoe/{dep}/agg/{agg}/cmd`                     | to device | no |
| `eoe/{dep}/agg/{agg}/lst/{mac}/desired`       | to device | yes |
| `eoe/{dep}/agg/{agg}/lst/{mac}/reported`      | to platform | no |

Identifiers are validated, never trusted: a slug or MAC that reached a topic
string unchecked could inject a wildcard and hand one device another's
subtree. The formats below are exactly the database CHECK constraints on
`deployment.slug` and `listener.mac`, so a value that round-trips through
inventory always builds a legal topic. Values arrive here ALREADY NORMALIZED
(`app.inventory.naming.normalize_mac` does that once, at the API boundary);
this module rejects rather than repairs.
"""

import re
from typing import Final

ROOT: Final = "eoe"

#: Retained per spec 7.2 so a reconnecting Aggregator gets desired state with
#: no polling; QoS 1 on every platform topic (phase-3 fixed choice).
QOS: Final = 1

_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_AGGREGATOR_UUID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class TopicError(ValueError):
    """An identifier that cannot legally appear in a topic string.

    Deliberately a plain ValueError and not the API's AppError: this module is
    a wire contract consumed outside the HTTP layer (SIM, the worker), and it
    must not drag the error envelope along with it.
    """


def _slug(value: str) -> str:
    if not _SLUG_RE.match(value):
        raise TopicError(f"illegal deployment slug for a topic: {value!r}")
    return value


def _aggregator(value: str) -> str:
    if not _AGGREGATOR_UUID_RE.match(value):
        raise TopicError(f"illegal aggregator_uuid for a topic: {value!r}")
    return value


def _mac(value: str) -> str:
    if not _MAC_RE.match(value):
        raise TopicError(
            f"MAC {value!r} is not in normalized AA:BB:CC:DD:EE:FF form; "
            "normalize at the API boundary with app.inventory.naming.normalize_mac"
        )
    return value


def deployment_root(dep: str) -> str:
    """`eoe/{dep}` — everything for one deployment lives under here, which is
    also the boundary the platform account's broker ACL is cut to (spec 7.1)."""
    return f"{ROOT}/{_slug(dep)}"


def aggregator_root(dep: str, agg: str) -> str:
    """`eoe/{dep}/agg/{agg}` — one Aggregator's subtree, the boundary its own
    per-device credential is restricted to (spec 7.1)."""
    return f"{deployment_root(dep)}/agg/{_aggregator(agg)}"


def desired_topic(dep: str, agg: str) -> str:
    return f"{aggregator_root(dep, agg)}/desired"


def reported_topic(dep: str, agg: str) -> str:
    return f"{aggregator_root(dep, agg)}/reported"


def status_topic(dep: str, agg: str) -> str:
    """The LWT topic: the broker publishes `offline` here on unexpected
    disconnect, which is what makes offline detection free (spec 7.2)."""
    return f"{aggregator_root(dep, agg)}/status"


def event_topic(dep: str, agg: str) -> str:
    return f"{aggregator_root(dep, agg)}/event"


def command_topic(dep: str, agg: str) -> str:
    return f"{aggregator_root(dep, agg)}/cmd"


def listener_root(dep: str, agg: str, mac: str) -> str:
    """Listeners never connect to MQTT (spec 6.4): their subtopics hang off
    the Aggregator that holds their config and reports on their behalf."""
    return f"{aggregator_root(dep, agg)}/lst/{_mac(mac)}"


def listener_desired_topic(dep: str, agg: str, mac: str) -> str:
    return f"{listener_root(dep, agg, mac)}/desired"


def listener_reported_topic(dep: str, agg: str, mac: str) -> str:
    return f"{listener_root(dep, agg, mac)}/reported"


def deployment_subscriptions(dep: str) -> tuple[str, ...]:
    """Every device-to-platform topic in one deployment, as the wildcard
    filters the platform subscribes to (E3.2/E3.5). Deliberately NOT
    `eoe/{dep}/#`: subscribing to the desired and cmd topics too would echo
    the platform's own retained publishes back into its consumer."""
    root = deployment_root(dep)
    return (
        f"{root}/agg/+/reported",
        f"{root}/agg/+/status",
        f"{root}/agg/+/event",
        f"{root}/agg/+/lst/+/reported",
    )
