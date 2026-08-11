"""The Mosquitto dynamic security control channel (`$CONTROL/dynamic-security/v1`).

**Created by E5.4a, which needs the probe; extended by E5.6, which needs mint
and revoke.** The phase document names this module under E5.6 and it is built
one unit early on the owner's decision, for one reason: E5.4a's dynsec probe
and E5.6's credential minting are the same request/response round trip over the
same reserved topic, and two implementations of it would exist between now and
then. Nothing about E5.6's scope moves here - `BrokerCredentialProvider`, the
`broker_credential` table and the mint/revoke endpoints are still E5.6's.

**Why a dedicated short-lived client and never `MqttClientManager`** (phase
document section 4, E5.6): the manager's subscription set is fixed before
`start()` (D64) and `$CONTROL` is not in `deployment_subscriptions`; the probe
runs against CANDIDATE coordinates that may not be stored yet, while the
manager only knows deployments that already have a row; and correlating a reply
wants a fresh session, so a stale response cannot be mistaken for this call's
answer.

## The three verdicts, and what actually distinguishes them

Fixed choice 4 makes dynsec required for v1, so the probe's verdict is part of
broker verification and a boolean would be wrong: "the plugin is not installed"
and "the plugin is installed and this account may not drive it" are different
operator actions. The discriminator is **the SUBACK on the response topic**,
which was established by experiment against a real broker rather than reasoned
about, because the intuitive discriminator does not work:

| broker                              | SUBACK          | reply | verdict     |
| ----------------------------------- | --------------- | ----- | ----------- |
| `acl_file`, no plugin               | Granted QoS 1   | none  | `absent`    |
| dynsec, client holding `admin`      | Granted QoS 1   | yes   | `available` |
| dynsec, client without `admin`      | **Not authorized** | none | `denied`   |

The load-bearing surprise is the first row. A Mosquitto using `acl_file`
**grants** a subscription to a topic the file never mentions and then silently
refuses the matching publish (PUBACK reason 135), so "the publish was refused"
cannot tell a missing plugin from an unprivileged account. Dynsec, by contrast,
refuses the SUBSCRIBE itself. So a denied SUBACK means something is enforcing
ACLs on the dynsec control topics, which is the plugin; a granted SUBACK
followed by silence means nothing is listening on them at all.

That also makes the phase document's "the probe never publishes to `$CONTROL`
on a broker it has not first confirmed accepts the connection" structural
rather than a rule to remember: a refused SUBACK returns before the publish
line is reached.
"""

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import aiomqtt

from app.contracts.mqtt import QOS

logger = logging.getLogger(__name__)

#: The reserved topic the plugin listens on, and the one it answers on. Both
#: are Mosquitto's, fixed by the plugin and not ours to choose - which is why
#: they are literals here and topic BUILDERS everywhere else in the codebase.
CONTROL_TOPIC = "$CONTROL/dynamic-security/v1"
RESPONSE_TOPIC = f"{CONTROL_TOPIC}/response"

#: How long to wait for the plugin to answer. Short: the plugin replies from
#: memory, so anything slower than a round trip is silence with extra steps,
#: and the probe is one of three checks inside the MQTT tester's own budget.
DEFAULT_RESPONSE_TIMEOUT = 3.0

#: `available` - the plugin answered, and this account may drive it.
#: `denied`   - something is enforcing ACLs on the control topics (so the
#:              plugin is there) and this account is not an administrator.
#: `absent`   - the broker took the subscription and nothing ever answered:
#:              no plugin is loaded.
DynsecVerdict = Literal["available", "denied", "absent"]


class DynsecError(Exception):
    """A dynsec command was refused or answered with an error.

    Carries the plugin's own `error` string, which is safe to surface: it
    names commands, clients and roles, and never a password, because the
    plugin never echoes one back.
    """

    def __init__(self, command: str, error: str) -> None:
        super().__init__(f"{command}: {error}")
        self.command = command
        self.error = error


@dataclass(frozen=True)
class DynsecProbe:
    """What the probe concluded, and what the operator should do about it.

    `remedy` lives here rather than in the tester because the remedy is a
    property of the verdict: fixed choice 4's cost is that an operator running
    a plugin-less Mosquitto has to enable it, and this is the sentence that
    tells them so.
    """

    verdict: DynsecVerdict
    detail: str
    remedy: str

    @property
    def usable(self) -> bool:
        """Whether the platform can mint per-device credentials here.

        Fixed choice 4: dynsec is required, so only `available` passes. E5.6
        reads this before attempting a mint; E5.4a fails the tester on it.
        """
        return self.verdict == "available"


_ABSENT = DynsecProbe(
    verdict="absent",
    detail=(
        "the broker accepted a subscription to the dynamic-security control topic and "
        "nothing answered, so the dynamic security plugin is not loaded"
    ),
    remedy=(
        "enable Mosquitto's dynamic security plugin on this broker - add "
        "'plugin /usr/lib/mosquitto_dynamic_security.so' and "
        "'plugin_opt_config_file <path>/dynamic-security.json' to mosquitto.conf, "
        "initialise the file with 'mosquitto_ctrl dynsec init', and restart. The "
        "platform mints each device's own broker credentials through this plugin, so a "
        "broker without it cannot be verified"
    ),
)

_DENIED = DynsecProbe(
    verdict="denied",
    detail=(
        "the broker refused this account a subscription to the dynamic-security control "
        "topic, so the plugin is installed but this account is not one of its "
        "administrators"
    ),
    remedy=(
        "grant this account the dynamic security 'admin' role on the broker - "
        "'mosquitto_ctrl dynsec addClientRole <username> admin' - or give the platform "
        "an account that already holds it. The platform needs it to create each "
        "device's own credentials and ACLs"
    ),
)


def _available(default_acl_access: object) -> DynsecProbe:
    return DynsecProbe(
        verdict="available",
        detail=(
            "the dynamic security plugin answered getDefaultACLAccess and this account "
            f"is authorised to drive it ({_summarise_acl_access(default_acl_access)})"
        ),
        remedy="",
    )


def _summarise_acl_access(data: object) -> str:
    """A one-line rendering of the plugin's answer, for the operator.

    Defensive about the shape on purpose: the verdict must not depend on the
    plugin's reply matching a schema we wrote down, only on it being a
    well-formed answer to the command we sent. A future Mosquitto that adds a
    field must not turn `available` into a crash.
    """
    if not isinstance(data, dict):
        return "no default ACL detail returned"
    acls = data.get("acls")
    if not isinstance(acls, list):
        return "no default ACL detail returned"
    parts = [
        f"{entry.get('acltype')}={'allow' if entry.get('allow') else 'deny'}"
        for entry in acls
        if isinstance(entry, dict)
    ]
    return ", ".join(parts) if parts else "no default ACL detail returned"


async def _next_response(client: aiomqtt.Client, timeout: float) -> dict[str, Any] | None:
    """The next well-formed message on the response topic, or None on timeout.

    Filters by topic because the caller's client may hold other subscriptions
    - E5.4a's tester runs its own publish/subscribe round trip on the same
    connection - and `client.messages` is one queue for all of them. A message
    that is not ours is discarded rather than treated as an answer.
    """
    try:
        async with asyncio.timeout(timeout):
            async for message in client.messages:
                if not message.topic.matches(RESPONSE_TOPIC):
                    continue
                try:
                    payload = json.loads(bytes(message.payload))
                except (TypeError, ValueError):
                    # The plugin does not send malformed JSON; something else
                    # publishing on its response topic is not an answer.
                    logger.warning("dynsec response topic carried a non-JSON payload")
                    continue
                if isinstance(payload, dict):
                    return payload
    except TimeoutError:
        return None
    return None


async def _subscribe_to_responses(client: aiomqtt.Client) -> bool:
    """Subscribe to the response topic; False if the broker refused.

    The refusal is the whole discriminator (see the module docstring), so it
    is read from the SUBACK reason codes rather than inferred from silence
    later. aiomqtt surfaces them for both MQTT 3.1.1 (`Unspecified error`,
    0x80) and MQTT 5 (`Not authorized`, 0x87); `is_failure` covers both.
    """
    try:
        codes = await client.subscribe(RESPONSE_TOPIC, qos=QOS)
    except aiomqtt.MqttError:
        # Some brokers drop the connection rather than refusing the
        # subscription. Indistinguishable from a refusal, and the remedy for
        # "you may not touch the control topics" is the right one to offer.
        return False
    return not any(getattr(code, "is_failure", False) for code in codes)


async def call(
    client: aiomqtt.Client,
    commands: Sequence[dict[str, Any]],
    *,
    timeout: float = DEFAULT_RESPONSE_TIMEOUT,
    subscribed: bool = False,
) -> list[dict[str, Any]]:
    """Run dynsec commands over an already-connected client and return the
    plugin's responses, in the order it answered.

    **E5.6's entry point.** `mint` and `revoke` are `createClient` /
    `deleteClient` plus role and ACL commands, and they need exactly this: one
    publish, one correlated reply, errors raised rather than returned.

    Pass `subscribed=True` when the caller has already subscribed to the
    response topic (E5.4a's probe has, and re-subscribing would be a second
    SUBACK to interpret).

    Raises `DynsecError` if the plugin reports an error for any command, and
    `TimeoutError` if it never answers - both of which are real failures for a
    mint, unlike the probe, which is asking a question whose "no" is an
    answer.
    """
    if not subscribed and not await _subscribe_to_responses(client):
        raise DynsecError(
            commands[0].get("command", "?") if commands else "?",
            "the broker refused a subscription to the dynamic security response topic; "
            "this account is not a dynamic security administrator",
        )
    await client.publish(CONTROL_TOPIC, json.dumps({"commands": list(commands)}).encode(), qos=QOS)
    payload = await _next_response(client, timeout)
    if payload is None:
        raise TimeoutError(f"the dynamic security plugin did not answer within {timeout:g}s")
    responses = payload.get("responses")
    if not isinstance(responses, list):
        raise DynsecError("?", "the dynamic security plugin sent a reply with no responses")
    out: list[dict[str, Any]] = []
    for response in responses:
        if not isinstance(response, dict):
            continue
        if "error" in response:
            raise DynsecError(str(response.get("command", "?")), str(response["error"]))
        out.append(response)
    return out


async def probe(
    client: aiomqtt.Client, *, timeout: float = DEFAULT_RESPONSE_TIMEOUT
) -> DynsecProbe:
    """Ask an already-connected broker whether the platform can drive dynsec.

    Takes a CONNECTED client rather than dialling one, so the caller's
    connection is proof the broker accepted these credentials before anything
    is published to a reserved topic (phase document, E5.4a).

    `getDefaultACLAccess` is the command because it is the cheapest read the
    plugin offers: it takes no arguments, changes nothing, and a broker whose
    operator is watching their logs sees a read rather than a write.
    """
    if not await _subscribe_to_responses(client):
        return _DENIED
    await client.publish(
        CONTROL_TOPIC,
        json.dumps({"commands": [{"command": "getDefaultACLAccess"}]}).encode(),
        qos=QOS,
    )
    payload = await _next_response(client, timeout)
    if payload is None:
        return _ABSENT
    responses = payload.get("responses")
    if not isinstance(responses, list) or not responses:
        return _ABSENT
    answer = responses[0]
    if not isinstance(answer, dict):
        return _ABSENT
    if "error" in answer:
        # The plugin is unambiguously present - it answered - and it refused.
        # Reported as `denied` with the plugin's own words, because a reply
        # that says why beats the generic remedy.
        return DynsecProbe(
            verdict="denied",
            detail=(
                "the dynamic security plugin answered getDefaultACLAccess with an error: "
                f"{answer['error']}"
            ),
            remedy=_DENIED.remedy,
        )
    return _available(answer.get("data"))
