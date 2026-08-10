"""Gate 39 onward: the MQTT wire contract (tasks E3.1, E3.3; spec 7.2, 7.3).

`app.contracts.mqtt` is a PUBLISHED INTERFACE — SIM imports it and firmware is
written against it — so this suite is the contract's documentation as much as
its verification. The topic half lands with E3.1 because the broker ACL
generator builds its grants from these same builders rather than repeating the
namespace; E3.3 adds the payload models below it.
"""

import pytest

from app.contracts.mqtt import (
    QOS,
    ROOT,
    TopicError,
    aggregator_root,
    command_topic,
    deployment_root,
    deployment_subscriptions,
    desired_topic,
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
