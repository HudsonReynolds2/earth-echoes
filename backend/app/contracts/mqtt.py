"""The MQTT control-plane contract (tasks E3.1, E3.3; spec 7.2 and 7.3).

**PUBLISHED INTERFACE — the simulation harness (SIM epic) imports this module
and device firmware is written against it.** Additive change only: renaming a
topic builder or a payload field breaks devices in the field, and a snapshot
whose bytes change breaks the E2 checksum contract (D52).

Two halves: the spec 7.2 topic builders, and the spec 7.3 payloads as Pydantic
models. Together they are the whole wire surface between the platform and a
device: nothing else in the codebase should build a topic string or a
control-plane JSON body by hand — or take one apart, which is what
`parse_topic` is for (E3.5). Building and parsing live together so that the
identifier rules below are applied in both directions by the same code.

Which direction a payload travels decides how strict it is (D67). Models the
platform PUBLISHES (`DesiredConfig`, `Command`) forbid unknown fields, because
an unexpected key there is a bug on this side about to reach every device.
Models the platform RECEIVES (the reported states, `StatusMessage`,
`DeviceEvent`) ignore unknown fields, because firmware that adds a field must
not be able to make the platform stop reading its reports. `encode()` omits
absent optionals rather than sending nulls, which is what spec 7.3 means by
"present only while sleeping".

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

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    StringConstraints,
    ValidationError,
    model_validator,
)

ROOT: Final = "eoe"

#: Retained per spec 7.2 so a reconnecting Aggregator gets desired state with
#: no polling; QoS 1 on every platform topic (phase-3 fixed choice).
QOS: Final = 1

#: The `schema_version` every spec 7.3 payload carries. There is exactly one
#: version, and a payload claiming another is REJECTED rather than guessed at:
#: a device speaking a shape this platform has never seen must fail loudly.
SCHEMA_VERSION: Final = 1

_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_AGGREGATOR_UUID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class ContractError(ValueError):
    """Anything that violates this wire contract.

    Deliberately a plain ValueError and not the API's AppError: this module is
    a wire contract consumed outside the HTTP layer (SIM, the worker), and it
    must not drag the error envelope along with it.
    """


class TopicError(ContractError):
    """An identifier that cannot legally appear in a topic string."""


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


class TopicKind(StrEnum):
    """Which spec 7.2 topic a string is. One member per row of the table in
    the module docstring, so `parse_topic` can answer "what is this" with a
    value a `match` statement covers exhaustively rather than with a tuple of
    optional fields the caller has to interpret."""

    AGGREGATOR_DESIRED = "aggregator_desired"
    AGGREGATOR_REPORTED = "aggregator_reported"
    AGGREGATOR_STATUS = "aggregator_status"
    AGGREGATOR_EVENT = "aggregator_event"
    AGGREGATOR_COMMAND = "aggregator_command"
    LISTENER_DESIRED = "listener_desired"
    LISTENER_REPORTED = "listener_reported"


@dataclass(frozen=True)
class ParsedTopic:
    """The identifiers a delivered topic carries.

    **This is where a report's identity comes from, and it is the only
    trustworthy source of it** (E3.5). None of the spec 7.3 inbound payloads
    carries an identity field, deliberately: the topic a message arrived on is
    authenticated by the broker's per-device ACL (spec 7.1), which restricts
    each Aggregator to its own subtree, while a payload field would be a
    self-declaration any device could write anything into.
    """

    kind: TopicKind
    dep: str
    agg: str
    #: Set for the two Listener kinds and None for the five Aggregator ones.
    mac: str | None = None


_AGGREGATOR_LEAVES: Final[dict[str, TopicKind]] = {
    "desired": TopicKind.AGGREGATOR_DESIRED,
    "reported": TopicKind.AGGREGATOR_REPORTED,
    "status": TopicKind.AGGREGATOR_STATUS,
    "event": TopicKind.AGGREGATOR_EVENT,
    "cmd": TopicKind.AGGREGATOR_COMMAND,
}

_LISTENER_LEAVES: Final[dict[str, TopicKind]] = {
    "desired": TopicKind.LISTENER_DESIRED,
    "reported": TopicKind.LISTENER_REPORTED,
}


def parse_topic(topic: str) -> ParsedTopic:
    """The inverse of the topic builders above, or `TopicError`.

    The identifiers are validated on the way IN by the same functions that
    validate them on the way out, which is the point: a topic is an untrusted
    string when it arrives from a broker, and a `+` or a `#` that survived
    parsing would let one device's subtree answer for another's. A subscriber
    receives only concrete topics, so a wildcard here means something is
    building topics by hand — refuse rather than repair.
    """
    parts = topic.split("/")
    if len(parts) not in (5, 7) or parts[0] != ROOT or parts[2] != "agg":
        raise TopicError(f"not a spec 7.2 topic: {topic!r}")
    dep, agg = _slug(parts[1]), _aggregator(parts[3])
    if len(parts) == 5:
        kind = _AGGREGATOR_LEAVES.get(parts[4])
        if kind is None:
            raise TopicError(
                f"{parts[4]!r} is not a spec 7.2 Aggregator topic leaf in {topic!r}; "
                f"expected one of {', '.join(sorted(_AGGREGATOR_LEAVES))}"
            )
        return ParsedTopic(kind=kind, dep=dep, agg=agg)
    if parts[4] != "lst":
        raise TopicError(f"expected an 'lst' segment in {topic!r}, found {parts[4]!r}")
    listener_kind = _LISTENER_LEAVES.get(parts[6])
    if listener_kind is None:
        raise TopicError(
            f"{parts[6]!r} is not a spec 7.2 Listener topic leaf in {topic!r}; "
            f"expected one of {', '.join(sorted(_LISTENER_LEAVES))}"
        )
    return ParsedTopic(kind=listener_kind, dep=dep, agg=agg, mac=_mac(parts[5]))


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


# ===========================================================================
# Section 7.3: the payloads
# ===========================================================================


class PayloadError(ContractError):
    """A payload that does not match its spec 7.3 model.

    **Safe to log verbatim.** It carries the model's name and which fields
    were wrong, and deliberately NOT the offending values: a config body holds
    secret markers and an event's detail is device-supplied text of unknown
    provenance, and Pydantic's own rendering echoes the input back (rule R2).
    """


def _to_utc(value: dt.datetime) -> dt.datetime:
    return value.astimezone(dt.UTC)


def _iso_z(value: dt.datetime) -> str:
    """RFC 3339 with a `Z`, the form every spec 7.3 example uses. Python's
    `isoformat` writes `+00:00`, which is the same instant and a different
    string — and firmware comparing timestamps as strings would notice."""
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


#: An instant on the wire. TIMEZONE-AWARE ONLY: a naive timestamp from a
#: device is ambiguous, and guessing UTC for it would silently mis-order
#: reports against the spec 7.4 staleness rule. Normalized to UTC on the way
#: in and serialized with `Z` on the way out.
UtcTimestamp = Annotated[
    AwareDatetime,
    AfterValidator(_to_utc),
    PlainSerializer(_iso_z, return_type=str, when_used="json"),
]

#: The D52 checksum recipe's output shape. The recipe itself lives in
#: `app.config.canonical` and is deliberately NOT imported here: this module
#: is published to firmware authors, who implement the recipe rather than call
#: it, and the wire contract only needs to say what the field looks like.
Checksum = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

#: A Listener MAC in the same normalized form the topic builders demand.
MacAddress = Annotated[str, StringConstraints(pattern=_MAC_RE.pattern)]

#: A machine-readable event key. Open vocabulary — spec 7.2 calls the event
#: topic "logs, errors, lifecycle events" and firmware will add codes the
#: platform has never heard of — but constrained in SHAPE so codes stay usable
#: as identifiers in queries, alert rules and UI copy.
EventCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]

#: Spec 6.5's three Listener liveness states. `streaming` and `sleeping` both
#: read as healthy (spec 9.3); only `offline` — a missed wake window past
#: grace — is a fault.
LivenessState = Literal["streaming", "sleeping", "offline"]

#: The spec 7.2 command list, verbatim.
CommandName = Literal["restart", "resync", "flush_buffer"]

#: Named because the platform reacts to them specifically (E3.9): the first is
#: spec 7.3's stream-gap event, the second is spec 6.5's missed wake window,
#: which is what flips a sleeping Listener to offline.
EVENT_LISTENER_STREAM_GAP: Final = "listener_stream_gap"
EVENT_LISTENER_MISSED_WAKE_WINDOW: Final = "listener_missed_wake_window"

#: Device-supplied free text is bounded here rather than at the database, so
#: firmware authors read the budget off the contract instead of discovering it
#: from a truncated row. Truncate at the source; the platform rejects rather
#: than silently shortening (D67).
DETAIL_MAX = 2000


class _NestedOut(BaseModel):
    """A block INSIDE an outbound payload. `schema_version` is top-level only
    (spec 7.3), so nested blocks deliberately do not inherit it."""

    model_config = ConfigDict(extra="forbid")


class _NestedIn(BaseModel):
    """A block inside an inbound payload; see `_NestedOut`."""

    model_config = ConfigDict(extra="ignore")


class MqttPayload(BaseModel):
    """Base of every spec 7.3 body: the top-level `schema_version`.

    Absent means 1 — there has never been another version, so a device that
    omits the field can only have meant this one. A value other than 1 is a
    hard error; see SCHEMA_VERSION.
    """

    schema_version: Literal[1] = SCHEMA_VERSION


class _Outbound(MqttPayload):
    """Platform to device. Unknown fields are a bug on this side."""

    model_config = ConfigDict(extra="forbid")


class _Inbound(MqttPayload):
    """Device to platform. Unknown fields are firmware moving ahead of the
    platform, which must never stop the platform reading the rest."""

    model_config = ConfigDict(extra="ignore")


# --- Desired config (platform to device, retained) -------------------------


class DesiredTarget(_NestedOut):
    """Which device this revision is for. The vocabulary matches
    `config_revision.target_type` (D55) because a revision IS the payload:
    aggregators are addressed by `aggregator_uuid`, listeners by MAC."""

    type: Literal["aggregator", "listener"]
    id: str = Field(min_length=1, max_length=100)


class DesiredConfig(_Outbound):
    """The retained body of `eoe/{dep}/agg/{agg}/desired` (and the Listener
    subtopic), spec 7.3.

    `config` is the `config_revision.snapshot` VERBATIM and `checksum` is that
    snapshot's D52 checksum. The snapshot is the publishable payload body, so
    a device that echoes the checksum of what it applied matches by
    construction — copying the snapshot through unchanged is load-bearing, not
    convenience. Values are secret MARKERS where a key is secret; plaintext
    secrets never transit a desired topic (spec 5.4, 8).
    """

    revision_id: uuid.UUID
    generated_at: UtcTimestamp
    target: DesiredTarget
    config: dict[str, JsonValue]
    checksum: Checksum


# --- Reported state (device to platform) -----------------------------------


class HealthBlock(_NestedIn):
    """Spec 7.3's coarse, best-effort liveness hint. NOT metrics: Prometheus
    is the authoritative source for CPU, memory, disk and queue depth (spec
    10.1), and the platform neither charts these numbers nor stores them as a
    time series. `coarse` is free text on purpose (D67) — inventing a
    vocabulary firmware has not agreed to would reject real reports."""

    uptime_s: int | None = Field(default=None, ge=0)
    coarse: str | None = Field(default=None, max_length=40)


class ReportedAggregatorState(_Inbound):
    """The body of `eoe/{dep}/agg/{agg}/reported`, spec 7.3.

    `applied_revision_id` may be absent: a device that has applied nothing yet
    still reports its state. Spec 7.4 makes these messages idempotent by
    `applied_revision_id` plus `checksum`, which is why both are here rather
    than the platform re-deriving a checksum from `config` — the device's own
    checksum is what proves WHAT it applied, not what it happens to hold now.
    """

    reported_at: UtcTimestamp
    applied_revision_id: uuid.UUID | None = None
    config: dict[str, JsonValue] = Field(default_factory=dict)
    health: HealthBlock | None = None
    checksum: Checksum


class ListenerLiveness(_NestedIn):
    """Spec 6.5's liveness block, as tracked by the Aggregator.

    `expected_wake_at` is present exactly while sleeping, and the model
    enforces both halves of that. The Listener declares its own wake time over
    the local link and the Aggregator trusts it; the PLATFORM never recomputes
    a wake schedule (spec 6.5), so a `sleeping` report with no wake time would
    leave it with no way to tell healthy sleep from silence, and a wake time
    on a `streaming` report would be a stale value it might act on.
    """

    state: LivenessState
    last_audio_at: UtcTimestamp | None = None
    expected_wake_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def _wake_time_belongs_to_sleeping(self) -> "ListenerLiveness":
        if self.state == "sleeping" and self.expected_wake_at is None:
            raise ValueError("a sleeping Listener must declare expected_wake_at (spec 6.5)")
        if self.state != "sleeping" and self.expected_wake_at is not None:
            raise ValueError(f"expected_wake_at is meaningless while {self.state} (spec 7.3)")
        return self


class ReportedListenerState(_Inbound):
    """The body of `.../lst/{mac}/reported`, published by the AGGREGATOR on the
    Listener's behalf — Listeners never hold an MQTT session (spec 6.4)."""

    reported_at: UtcTimestamp
    applied_revision_id: uuid.UUID | None = None
    config: dict[str, JsonValue] = Field(default_factory=dict)
    liveness: ListenerLiveness
    checksum: Checksum


# --- Status, events, commands ----------------------------------------------


class StatusMessage(_Inbound):
    """The retained body of `eoe/{dep}/agg/{agg}/status`, spec 7.2/7.3.

    The device publishes `online` itself and registers the `offline` form as
    its LWT, so the broker publishes it on an unexpected disconnect. This is
    the AUTHORITATIVE real-time liveness verdict for an Aggregator (spec 9.3);
    Prometheus is deliberately not, because its agent buffers and backfills.
    """

    state: Literal["online", "offline"]
    at: UtcTimestamp


class DeviceEvent(_Inbound):
    """The body of `eoe/{dep}/agg/{agg}/event`, spec 7.3.

    `listener_mac` is set when the event is about one Listener — both of the
    named codes are. E7 owns alerts; E3 persists these and shows them on the
    timeline, which is why `code` stays machine-readable and `detail` stays
    human-readable rather than the two being merged.
    """

    at: UtcTimestamp
    level: Literal["debug", "info", "warn", "error"]
    code: EventCode
    detail: str | None = Field(default=None, max_length=DETAIL_MAX)
    listener_mac: MacAddress | None = None


class Command(_Outbound):
    """The body of `eoe/{dep}/agg/{agg}/cmd`, spec 7.2/7.4.

    `command_id` defaults to a fresh UUID so that two submissions of one
    logical command carry distinct ids STRUCTURALLY, which is what lets a
    device deduplicate its own retries without deduplicating an operator's
    deliberate second attempt (spec 7.4). The topic is not retained: a command
    is a one-shot, and a retained one would re-fire on every reconnect.
    """

    command_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    at: UtcTimestamp
    command: CommandName


# --- Encoding ---------------------------------------------------------------


def encode(payload: MqttPayload) -> bytes:
    """The publishable bytes of a spec 7.3 body.

    Absent optionals are OMITTED rather than sent as null, which is what spec
    7.3 means by "present only while sleeping" and keeps the payloads small
    over links that are sometimes cellular.
    """
    return payload.model_dump_json(exclude_none=True).encode("utf-8")


def decode[PayloadT: MqttPayload](model: type[PayloadT], raw: bytes | str) -> PayloadT:
    """Parse one received payload, raising `PayloadError` on anything that
    does not fit. Consumers (E3.5) catch that one exception rather than
    Pydantic's, so the wire contract does not leak its implementation into
    every caller's except clause.

    The message names the fields that failed and not their values — see
    `PayloadError`. `include_input=False` is what makes that true, so a later
    edit that drops it for a nicer message would put secret markers into logs.
    """
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'body'}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False, include_context=False)
        )
        raise PayloadError(f"payload is not a valid {model.__name__}: {problems}") from error


def describe(payload: MqttPayload) -> dict[str, Any]:
    """The payload as a plain JSON-compatible dict (same rules as `encode`).

    For logs, audit rows and timeline detail, where a JSON string would have
    to be parsed straight back out again.
    """
    return payload.model_dump(mode="json", exclude_none=True)
