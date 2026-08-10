"""Gate 39 onward: the MQTT wire contract (tasks E3.1, E3.3; spec 7.2, 7.3).

`app.contracts.mqtt` is a PUBLISHED INTERFACE — SIM imports it and firmware is
written against it — so this suite is the contract's documentation as much as
its verification. The topic half lands with E3.1 because the broker ACL
generator builds its grants from these same builders rather than repeating the
namespace; E3.3 adds the payload models below it.
"""

import datetime as dt
import json
import uuid

import pytest

from app.config.canonical import canonical_config_bytes, config_checksum
from app.contracts.mqtt import (
    DETAIL_MAX,
    EVENT_LISTENER_MISSED_WAKE_WINDOW,
    EVENT_LISTENER_STREAM_GAP,
    QOS,
    ROOT,
    SCHEMA_VERSION,
    Command,
    ContractError,
    DesiredConfig,
    DesiredTarget,
    DeviceEvent,
    HealthBlock,
    ListenerLiveness,
    MqttPayload,
    PayloadError,
    ReportedAggregatorState,
    ReportedListenerState,
    StatusMessage,
    TopicError,
    aggregator_root,
    command_topic,
    decode,
    deployment_root,
    deployment_subscriptions,
    describe,
    desired_topic,
    encode,
    event_topic,
    listener_desired_topic,
    listener_reported_topic,
    listener_root,
    reported_topic,
    status_topic,
)

DEP = "redwood-coast"
AGG = "demo-agg-rc-01"
MAC = "02:EE:0E:01:01:01"

#: The spec 7.3 examples write `"sha256:..."`; the ellipsis stands in for a
#: digest, so the fixtures below use a real one of the right shape.
DIGEST = "sha256:" + "ab12" * 16
WHEN = dt.datetime(2026, 6, 23, 12, 0, 0, tzinfo=dt.UTC)


def test_the_topic_table_is_exactly_spec_7_2():
    """One assertion per row of the spec 7.2 table, spelled out literally: a
    builder that silently changes shape orphans every device in the field."""
    assert desired_topic(DEP, AGG) == "eoe/redwood-coast/agg/demo-agg-rc-01/desired"
    assert reported_topic(DEP, AGG) == "eoe/redwood-coast/agg/demo-agg-rc-01/reported"
    assert status_topic(DEP, AGG) == "eoe/redwood-coast/agg/demo-agg-rc-01/status"
    assert event_topic(DEP, AGG) == "eoe/redwood-coast/agg/demo-agg-rc-01/event"
    assert command_topic(DEP, AGG) == "eoe/redwood-coast/agg/demo-agg-rc-01/cmd"
    assert (
        listener_desired_topic(DEP, AGG, MAC)
        == "eoe/redwood-coast/agg/demo-agg-rc-01/lst/02:EE:0E:01:01:01/desired"
    )
    assert (
        listener_reported_topic(DEP, AGG, MAC)
        == "eoe/redwood-coast/agg/demo-agg-rc-01/lst/02:EE:0E:01:01:01/reported"
    )


def test_every_topic_hangs_off_the_deployment_root():
    root = deployment_root(DEP)
    assert root == f"{ROOT}/{DEP}"
    for topic in (
        desired_topic(DEP, AGG),
        reported_topic(DEP, AGG),
        status_topic(DEP, AGG),
        event_topic(DEP, AGG),
        command_topic(DEP, AGG),
        listener_desired_topic(DEP, AGG, MAC),
        listener_reported_topic(DEP, AGG, MAC),
    ):
        assert topic.startswith(f"{root}/"), topic
        assert topic.startswith(f"{aggregator_root(DEP, AGG)}/"), topic


def test_listener_topics_hang_off_their_aggregator():
    """Listeners never connect to MQTT (spec 6.4) - their subtopics belong to
    the Aggregator that reports on their behalf, which is also why one device
    credential covers them."""
    assert listener_root(DEP, AGG, MAC).startswith(aggregator_root(DEP, AGG) + "/lst/")


@pytest.mark.parametrize(
    "slug",
    [
        "Redwood-Coast",  # uppercase
        "redwood coast",  # space
        "redwood/coast",  # a path separator would escape the namespace
        "redwood#",  # multi-level wildcard
        "redwood+",  # single-level wildcard
        "-redwood",  # the DB CHECK forbids a leading hyphen
        "",
    ],
)
def test_bad_slugs_are_rejected(slug):
    with pytest.raises(TopicError):
        deployment_root(slug)


@pytest.mark.parametrize(
    "mac",
    [
        "02:ee:0e:01:01:01",  # lowercase - normalize at the API boundary first
        "02-EE-0E-01-01-01",  # hyphen form
        "02EE0E010101",  # bare hex
        "02:EE:0E:01:01",  # too short
        "#",
        "",
    ],
)
def test_unnormalized_or_malformed_macs_are_rejected(mac):
    with pytest.raises(TopicError):
        listener_desired_topic(DEP, AGG, mac)


@pytest.mark.parametrize("agg", ["with/slash", "with+plus", "with#hash", "", "x" * 65])
def test_bad_aggregator_ids_are_rejected(agg):
    with pytest.raises(TopicError):
        aggregator_root(DEP, agg)


def test_platform_subscriptions_cover_device_to_platform_topics_only():
    """Subscribing to `eoe/{dep}/#` would feed the platform's own retained
    desired publishes straight back into its consumer."""
    filters = deployment_subscriptions(DEP)
    root = deployment_root(DEP)
    assert filters == (
        f"{root}/agg/+/reported",
        f"{root}/agg/+/status",
        f"{root}/agg/+/event",
        f"{root}/agg/+/lst/+/reported",
    )
    for wildcard in filters:
        assert "/desired" not in wildcard
        assert "/cmd" not in wildcard


def test_platform_topics_are_qos_1():
    """Phase-3 fixed choice: QoS 1 on every platform topic, so an at-least-once
    delivery is what the idempotency rules in spec 7.4 are written against."""
    assert QOS == 1


# ===========================================================================
# E3.3: the spec 7.3 payloads
# ===========================================================================


def desired(**overrides) -> DesiredConfig:
    fields = {
        "revision_id": uuid.uuid4(),
        "generated_at": WHEN,
        "target": DesiredTarget(type="aggregator", id=AGG),
        "config": {"logging.verbosity": "info", "analysis.confidence_threshold": 0.6},
        "checksum": DIGEST,
    }
    return DesiredConfig(**{**fields, **overrides})


def reported(**overrides) -> ReportedAggregatorState:
    fields = {
        "reported_at": WHEN,
        "applied_revision_id": uuid.uuid4(),
        "config": {"logging.verbosity": "info"},
        "health": HealthBlock(uptime_s=86400, coarse="ok"),
        "checksum": DIGEST,
    }
    return ReportedAggregatorState(**{**fields, **overrides})


def listener_reported(**overrides) -> ReportedListenerState:
    fields = {
        "reported_at": WHEN,
        "applied_revision_id": uuid.uuid4(),
        "config": {"audio.sample_rate_hz": 48000},
        "liveness": ListenerLiveness(state="streaming", last_audio_at=WHEN),
        "checksum": DIGEST,
    }
    return ReportedListenerState(**{**fields, **overrides})


# --- The spec's own examples, parsed ----------------------------------------


def test_the_spec_7_3_examples_parse_as_written():
    """Every payload shape spec 7.3 prints, decoded by its model. If a later
    edit renames a field, this is the test that says which document it just
    contradicted."""
    assert (
        decode(
            DesiredConfig,
            json.dumps(
                {
                    "schema_version": 1,
                    "revision_id": str(uuid.uuid4()),
                    "generated_at": "2026-06-23T12:00:00Z",
                    "target": {"type": "aggregator", "id": "agg-uuid"},
                    "config": {"logging.verbosity": "info", "analysis.confidence_threshold": 0.6},
                    "checksum": DIGEST,
                }
            ),
        ).target.id
        == "agg-uuid"
    )

    state = decode(
        ReportedAggregatorState,
        json.dumps(
            {
                "schema_version": 1,
                "reported_at": "2026-06-23T12:00:05Z",
                "applied_revision_id": str(uuid.uuid4()),
                "config": {"logging.verbosity": "info", "analysis.confidence_threshold": 0.6},
                "health": {"uptime_s": 86400, "coarse": "ok"},
                "checksum": DIGEST,
            }
        ),
    )
    assert state.health is not None and state.health.uptime_s == 86400

    listener = decode(
        ReportedListenerState,
        json.dumps(
            {
                "schema_version": 1,
                "reported_at": "2026-06-23T12:00:05Z",
                "applied_revision_id": str(uuid.uuid4()),
                "config": {"audio.sample_rate_hz": 48000},
                "liveness": {
                    "state": "sleeping",
                    "last_audio_at": "2026-06-23T11:58:00Z",
                    "expected_wake_at": "2026-06-23T12:05:00Z",
                },
                "checksum": DIGEST,
            }
        ),
    )
    assert listener.liveness.expected_wake_at == dt.datetime(2026, 6, 23, 12, 5, tzinfo=dt.UTC)

    assert (
        decode(
            StatusMessage,
            '{"schema_version": 1, "state": "offline", "at": "2026-06-23T12:01:00Z"}',
        ).state
        == "offline"
    )

    for code, at in (
        (EVENT_LISTENER_STREAM_GAP, "2026-06-23T12:00:30Z"),
        (EVENT_LISTENER_MISSED_WAKE_WINDOW, "2026-06-23T12:05:31Z"),
    ):
        event = decode(
            DeviceEvent,
            json.dumps(
                {
                    "schema_version": 1,
                    "at": at,
                    "level": "warn",
                    "code": code,
                    "detail": "listener AA:BB:CC:DD:EE:FF gap 240ms",
                    "listener_mac": "AA:BB:CC:DD:EE:FF",
                }
            ),
        )
        assert event.code == code and event.listener_mac == "AA:BB:CC:DD:EE:FF"


# --- Round trips ------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        desired(),
        desired(target=DesiredTarget(type="listener", id=MAC)),
        reported(),
        reported(applied_revision_id=None, health=None, config={}),
        listener_reported(),
        listener_reported(
            liveness=ListenerLiveness(state="sleeping", last_audio_at=WHEN, expected_wake_at=WHEN)
        ),
        listener_reported(liveness=ListenerLiveness(state="offline")),
        StatusMessage(state="online", at=WHEN),
        StatusMessage(state="offline", at=WHEN),
        DeviceEvent(at=WHEN, level="warn", code=EVENT_LISTENER_STREAM_GAP, listener_mac=MAC),
        DeviceEvent(at=WHEN, level="error", code="apply_failed", detail="disk full"),
        Command(at=WHEN, command="restart"),
        Command(at=WHEN, command="resync"),
        Command(at=WHEN, command="flush_buffer"),
    ],
    ids=lambda p: f"{type(p).__name__}-{id(p) % 1000}",
)
def test_every_payload_round_trips_through_the_wire_form(payload):
    """The acceptance criterion: encode, ship, decode, and get the same object
    back. Both directions matter — SIM builds these and the platform reads
    them, and vice versa."""
    assert decode(type(payload), encode(payload)) == payload


def test_the_wire_form_omits_absent_optionals_rather_than_sending_null():
    """Spec 7.3 says `expected_wake_at` is "present only while sleeping" —
    present, not null. Firmware checking for the key's existence is entitled
    to that reading."""
    body = json.loads(encode(listener_reported(liveness=ListenerLiveness(state="offline"))))
    assert "expected_wake_at" not in body["liveness"]
    assert "last_audio_at" not in body["liveness"]
    assert body["liveness"] == {"state": "offline"}
    assert "health" not in json.loads(encode(reported(health=None)))


def test_omitting_optionals_does_not_reach_inside_the_config_object():
    """A null VALUE inside `config` is data, not an absent field. Stripping it
    would change the payload body and therefore its D52 checksum."""
    body = json.loads(encode(desired(config={"a.key": None, "b.key": 1})))
    assert body["config"] == {"a.key": None, "b.key": 1}


def test_a_snapshot_survives_the_round_trip_byte_identically():
    """THE CHECKSUM CONTRACT (D52, D55): `config` is the revision snapshot
    verbatim, so a device that recomputes the checksum of what it received
    must land on the value the platform put in the `checksum` field. Types are
    the fragile part — a bool arriving as 1, or an int as 1.0, changes the
    canonical bytes and every checksum comparison fails."""
    snapshot = {
        "analysis.confidence_threshold": 0.6,
        "audio.sample_rate_hz": 48000,
        "capture.mode": "duty_cycle",
        "logging.debug_enabled": False,
        "network.stream_key": "secret:config:listener:02:EE:0E:01:01:01:network.stream_key",
        "upload.retries": [1, 2, 5],
        "zzz.nested": {"deep": None},
    }
    payload = desired(config=snapshot, checksum=config_checksum(snapshot))
    decoded = decode(DesiredConfig, encode(payload))
    assert canonical_config_bytes(decoded.config) == canonical_config_bytes(snapshot)
    assert config_checksum(decoded.config) == decoded.checksum


# --- schema_version ---------------------------------------------------------


def test_schema_version_defaults_to_one_and_rides_every_payload():
    assert SCHEMA_VERSION == 1
    for payload in (desired(), reported(), StatusMessage(state="online", at=WHEN)):
        assert json.loads(encode(payload))["schema_version"] == 1


def test_a_payload_from_a_future_schema_version_is_rejected_not_guessed_at():
    body = json.dumps({"schema_version": 2, "state": "online", "at": "2026-06-23T12:00:00Z"})
    with pytest.raises(PayloadError):
        decode(StatusMessage, body)


def test_schema_version_is_top_level_only():
    """Spec 7.3 puts it on the body, not on every nested block; a `target` or
    `liveness` object carrying one would be a shape no example shows."""
    body = json.loads(encode(listener_reported()))
    assert "schema_version" not in body["target" if "target" in body else "liveness"]
    assert "schema_version" not in json.loads(encode(desired()))["target"]


# --- Timestamps -------------------------------------------------------------


def test_timestamps_serialize_with_a_z_not_an_offset():
    """Same instant, different string. Firmware comparing timestamps textually
    would see `+00:00` as a different value from every spec example."""
    body = json.loads(encode(StatusMessage(state="online", at=WHEN)))
    assert body["at"] == "2026-06-23T12:00:00Z"


def test_a_non_utc_timestamp_is_normalized_rather_than_preserved():
    decoded = decode(StatusMessage, '{"state": "online", "at": "2026-06-23T14:00:00+02:00"}')
    assert decoded.at == WHEN
    assert json.loads(encode(decoded))["at"] == "2026-06-23T12:00:00Z"


def test_a_naive_timestamp_is_refused():
    """An instant with no zone cannot be ordered against another device's
    report, and spec 7.4 orders reports by timestamp to drop stale ones.
    Guessing UTC would make that silently wrong instead of loudly broken."""
    with pytest.raises(PayloadError):
        decode(StatusMessage, '{"state": "online", "at": "2026-06-23T12:00:00"}')
    with pytest.raises(ValueError):
        StatusMessage(state="online", at=dt.datetime(2026, 6, 23, 12, 0))


# --- Field-level validation -------------------------------------------------


@pytest.mark.parametrize(
    "checksum",
    [
        "sha256:...",  # the spec's own placeholder is not a checksum
        "sha256:" + "AB12" * 16,  # uppercase; the D52 recipe emits lowercase
        "sha256:" + "ab12" * 15,  # short
        "md5:" + "ab12" * 16,
        "ab12" * 16,  # no algorithm prefix
        "",
    ],
)
def test_a_malformed_checksum_is_refused(checksum):
    """Reconciliation compares these by string equality (D52). A value that is
    not a checksum could only ever compare unequal, so a device would look
    permanently drifted for a reason nobody could see."""
    with pytest.raises(ValueError):
        desired(checksum=checksum)


@pytest.mark.parametrize("mac", ["02:ee:0e:01:01:01", "02-EE-0E-01-01-01", "02EE0E010101", "#"])
def test_an_unnormalized_mac_on_an_event_is_refused(mac):
    """Same rule as the topic builders: normalized at the boundary, never
    repaired here. An event whose MAC does not match a listener row would
    attach to nothing."""
    with pytest.raises(ValueError):
        DeviceEvent(at=WHEN, level="warn", code="x_gap", listener_mac=mac)


@pytest.mark.parametrize(
    "code", ["Listener_Gap", "listener gap", "listener-gap", "9lives", "", "x" * 65]
)
def test_an_event_code_must_stay_an_identifier(code):
    """The vocabulary is open — firmware will invent codes — but the SHAPE is
    not, because codes end up in queries, alert rules and UI copy."""
    with pytest.raises(ValueError):
        DeviceEvent(at=WHEN, level="warn", code=code)


def test_event_detail_is_bounded():
    """Device-supplied free text, bounded on the contract so firmware authors
    read the budget here rather than discovering it from a truncated row."""
    DeviceEvent(at=WHEN, level="info", code="ok", detail="x" * DETAIL_MAX)
    with pytest.raises(ValueError):
        DeviceEvent(at=WHEN, level="info", code="ok", detail="x" * (DETAIL_MAX + 1))


@pytest.mark.parametrize("level", ["warning", "WARN", "critical", ""])
def test_an_unknown_event_level_is_refused(level):
    with pytest.raises(ValueError):
        DeviceEvent(at=WHEN, level=level, code="ok")


@pytest.mark.parametrize("state", ["online", "offline", "degraded", "OFFLINE"])
def test_status_carries_exactly_the_spec_7_2_vocabulary(state):
    if state in ("online", "offline"):
        assert StatusMessage(state=state, at=WHEN).state == state
    else:
        with pytest.raises(ValueError):
            StatusMessage(state=state, at=WHEN)


# --- Listener liveness (spec 6.5) ------------------------------------------


def test_a_sleeping_listener_must_declare_when_it_will_be_back():
    """The platform never recomputes a wake schedule (spec 6.5) — it stores
    what the Aggregator reports. A sleeping report with no wake time would
    leave nothing to tell healthy sleep from silence."""
    with pytest.raises(ValueError, match="expected_wake_at"):
        ListenerLiveness(state="sleeping", last_audio_at=WHEN)


@pytest.mark.parametrize("state", ["streaming", "offline"])
def test_a_wake_time_outside_sleep_is_refused(state):
    """Spec 7.3: `expected_wake_at` is absent once the Listener resumes
    streaming or flips to offline. A leftover value is a stale promise the
    platform might act on."""
    with pytest.raises(ValueError, match="expected_wake_at"):
        ListenerLiveness(state=state, expected_wake_at=WHEN)


@pytest.mark.parametrize("state", ["asleep", "healthy", "SLEEPING", ""])
def test_the_liveness_vocabulary_is_exactly_the_three_spec_6_5_states(state):
    with pytest.raises(ValueError):
        ListenerLiveness(state=state)


# --- Direction decides strictness (D67) ------------------------------------


def test_a_device_may_add_fields_the_platform_has_never_heard_of():
    """Firmware moving ahead of the platform must not be able to make the
    platform stop reading its reports."""
    decoded = decode(
        StatusMessage,
        '{"schema_version": 1, "state": "online", "at": "2026-06-23T12:00:00Z", "fw": "2.1"}',
    )
    assert decoded.state == "online"
    assert "fw" not in json.loads(encode(decoded)), "an ignored field is not echoed back"


def test_an_unexpected_field_on_an_outbound_payload_is_a_hard_error():
    """The other direction is a bug on this side, about to reach every device
    in the deployment."""
    with pytest.raises(ValueError):
        DesiredConfig(
            revision_id=uuid.uuid4(),
            generated_at=WHEN,
            target=DesiredTarget(type="aggregator", id=AGG),
            config={},
            checksum=DIGEST,
            urgent=True,
        )
    with pytest.raises(ValueError):
        DesiredTarget(type="aggregator", id=AGG, name="Pod 01")


# --- Commands ---------------------------------------------------------------


def test_each_command_gets_its_own_id_without_the_caller_remembering_to_ask():
    """Spec 7.4: a device deduplicates RETRIES by command_id, so two
    deliberate submissions of one logical command must carry different ids or
    the second would be swallowed. Structural, not a caller's discipline."""
    ids = {Command(at=WHEN, command="restart").command_id for _ in range(50)}
    assert len(ids) == 50


@pytest.mark.parametrize("name", ["reboot", "flush buffer", "restart ", ""])
def test_an_unknown_command_is_refused(name):
    with pytest.raises(ValueError):
        Command(at=WHEN, command=name)


# --- The decode/describe helpers -------------------------------------------


def test_decode_raises_the_contract_error_not_pydantics():
    """E3.5 catches one exception type. Letting ValidationError escape would
    put Pydantic in every consumer's except clause and make the wire contract
    harder to reimplement."""
    with pytest.raises(PayloadError) as caught:
        decode(StatusMessage, b"not json at all")
    assert "StatusMessage" in str(caught.value)


def test_the_contract_errors_share_one_base():
    """A caller that wants to treat 'this does not fit the contract' uniformly
    catches ContractError; both halves are still plain ValueErrors."""
    for error_type in (TopicError, PayloadError):
        assert issubclass(error_type, ContractError)
        assert issubclass(error_type, ValueError)


def test_a_decode_failure_never_repeats_the_payload_back():
    """Config bodies carry secret MARKERS and event details are device text of
    unknown provenance; neither belongs in a log line or an exception (R2).
    Pydantic's own rendering DOES echo the input, which is exactly why decode
    rebuilds the message from `errors(include_input=False)` — E3.5 logs these.
    """
    marker = "secret:config:pod:abc:network.wifi_password"
    body = json.dumps(
        {
            "schema_version": 1,
            "reported_at": "2026-06-23T12:00:00Z",
            "config": {"network.wifi_password": marker},
            # checksum missing: the failure pydantic reports by quoting the
            # WHOLE body back, config included.
        }
    )
    with pytest.raises(PayloadError) as caught:
        decode(ReportedAggregatorState, body)
    message = str(caught.value)
    assert "secret:config" not in message and "wifi_password" not in message
    assert "checksum" in message, "the message must still say what was wrong"


def test_describe_returns_a_json_ready_dict_for_timelines_and_audit():
    described = describe(desired())
    assert described["generated_at"] == "2026-06-23T12:00:00Z"
    assert isinstance(described["revision_id"], str)
    json.dumps(described)  # E3.11 puts this in a JSONB column


def test_every_payload_model_shares_the_published_base():
    """SIM and the worker both switch on payload type; a model that skipped
    the base would also have skipped schema_version."""
    for model in (
        DesiredConfig,
        ReportedAggregatorState,
        ReportedListenerState,
        StatusMessage,
        DeviceEvent,
        Command,
    ):
        assert issubclass(model, MqttPayload)
