"""E4.4: per-listener files and mixed mode (spec 8.2 mode 2; phase-4 E4.4).

Acceptance:

1. **A mixed-mode bundle carries the pod file plus per-listener files for
   exactly the named MACs**, and the named devices take NO pod-file record.
   Every Listener gets exactly one card.
2. **A MAC outside the selected scope is rejected at generation**, not
   silently included or dropped.

Plus: a per-listener file is resolved at the Listener (its own overrides and
identity reach it), filenames are legal on Windows, no secret plaintext is in
any byte, and a Deployment bundle can mix modes Pod by Pod but cannot reach
into another Deployment.
"""

import pytest
import yaml
from conftest import ephemeral_postgres, make_kek
from sqlalchemy import select

from app.config.overrides import put_overrides
from app.db import create_session_factory
from app.models import Aggregator, Deployment, Listener, Pod
from app.provisioning.generate import (
    GenerationError,
    generate_for_deployment,
    generate_for_pod,
    listener_file_path,
    pod_file_path,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy

pytestmark = pytest.mark.integration

PSK = "e44-planted-psk-5d1a90-never-on-a-card"
STREAM = "e44-planted-stream-e2c4f1-never-on-a-card"

NETWORK = {
    "network.wifi_ssid": "e44-halow",
    "network.wifi_password": PSK,
    "network.stream_key": STREAM,
    "network.aggregator_ip": "10.44.0.1",
    "network.stream_endpoint": "10.44.0.1:8765/stream",
}


@pytest.fixture(scope="module")
def env():
    """The demo hierarchy with network values set on every Pod."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        store = SecretStore(factory, make_kek())
        with factory() as db:
            seed_demo_hierarchy(db)
            for pod in db.scalars(select(Pod)):
                put_overrides(db, store, "pod", str(pod.id), NETWORK)
            db.commit()
        yield factory, store


def _pods(db) -> list[Pod]:
    return list(db.scalars(select(Pod).order_by(Pod.name)))


def _macs(db, pod: Pod) -> list[str]:
    aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod.id))
    return list(
        db.scalars(
            select(Listener.mac)
            .where(Listener.aggregator_id == aggregator.id)
            .order_by(Listener.mac)
        )
    )


def _cards(files) -> dict[str, str]:
    """MAC → the archive path that card takes, asserting one card per MAC."""
    cards: dict[str, str] = {}
    for generated in files:
        for record in generated.records:
            assert record.target_id not in cards, f"{record.target_id} got two cards"
            cards[record.target_id] = generated.path
    return cards


# --- 1. Mixed mode --------------------------------------------------------------


def test_mixed_mode_carries_the_pod_file_and_exactly_the_named_listener_files(env):
    factory, store = env
    with factory() as db:
        pod = _pods(db)[0]
        macs = _macs(db, pod)
        named = frozenset(macs[:2])
        files = generate_for_pod(db, store, pod.id, "mixed", named)

    paths = [generated.path for generated in files]
    assert paths[0] == pod_file_path(pod.id)
    assert sorted(paths[1:]) == sorted(listener_file_path(pod.id, mac) for mac in named)

    cards = _cards(files)
    assert set(cards) == set(macs), "every Listener gets exactly one card"
    for mac in macs:
        expected = listener_file_path(pod.id, mac) if mac in named else pod_file_path(pod.id)
        assert cards[mac] == expected
    kinds = {r.target_id: r.file_kind for f in files for r in f.records}
    assert {kinds[mac] for mac in named} == {"listener_file"}
    assert {kinds[mac] for mac in set(macs) - named} == {"pod_file"}


def test_per_listener_mode_gives_every_listener_its_own_file(env):
    factory, store = env
    with factory() as db:
        pod = _pods(db)[0]
        macs = _macs(db, pod)
        files = generate_for_pod(db, store, pod.id, "per_listener")

    assert [f.path for f in files] == [listener_file_path(pod.id, mac) for mac in macs]
    for generated, mac in zip(files, macs, strict=True):
        assert yaml.safe_load(generated.content)["identity"]["mac"] == mac


def test_per_pod_mode_is_the_shared_file_alone(env):
    factory, store = env
    with factory() as db:
        pod = _pods(db)[0]
        files = generate_for_pod(db, store, pod.id, "per_pod")
    assert [f.path for f in files] == [pod_file_path(pod.id)]


# --- 2. Scope -------------------------------------------------------------------


def test_a_mac_from_another_pod_is_rejected_at_generation(env):
    factory, store = env
    with factory() as db:
        pod, other = _pods(db)[0], _pods(db)[1]
        stranger = _macs(db, other)[0]
        with pytest.raises(GenerationError, match=stranger):
            generate_for_pod(db, store, pod.id, "mixed", frozenset({stranger}))


def test_an_unknown_mac_is_rejected_at_generation(env):
    factory, store = env
    with factory() as db:
        pod = _pods(db)[0]
        with pytest.raises(GenerationError, match="02:00:00:00:E4:44"):
            generate_for_pod(
                db, store, pod.id, "mixed", frozenset({_macs(db, pod)[0], "02:00:00:00:E4:44"})
            )


@pytest.mark.parametrize(
    ("mode", "named", "message"),
    [
        ("per_pod", True, "takes no listener list"),
        ("per_listener", True, "takes no listener list"),
        ("mixed", False, "needs at least one listener"),
        ("zero_touch", False, "unknown mode"),
    ],
)
def test_a_mode_and_listener_list_that_disagree_are_refused(env, mode, named, message):
    factory, store = env
    with factory() as db:
        pod = _pods(db)[0]
        macs = frozenset(_macs(db, pod)[:1]) if named else frozenset()
        with pytest.raises(GenerationError, match=message):
            generate_for_pod(db, store, pod.id, mode, macs)


# --- What a per-listener card carries --------------------------------------------


def test_a_listener_file_is_resolved_at_the_listener(env):
    """The reason the mode exists: this device's own first-boot settings."""
    factory, store = env
    with factory() as db:
        pod = _pods(db)[0]
        mac = _macs(db, pod)[0]
        put_overrides(db, store, "listener", mac, {"audio.sample_rate_hz": 96000})
        db.commit()
        name = db.get(Listener, mac).name
        listener_card, pod_card = None, None
        for generated in generate_for_pod(db, store, pod.id, "mixed", frozenset({mac})):
            if generated.path == pod_file_path(pod.id):
                pod_card = yaml.safe_load(generated.content)
            else:
                listener_card = yaml.safe_load(generated.content)

    assert listener_card["audio"]["sample_rate_hz"] == 96000
    assert listener_card["identity"] == {"name": name, "mac": mac}
    assert pod_card["audio"]["sample_rate_hz"] == 48000
    assert pod_card["identity"] == {"name": None, "mac": None}


def test_filenames_are_legal_on_windows_and_no_secret_is_in_any_byte(env):
    factory, store = env
    with factory() as db:
        pod = _pods(db)[0]
        files = generate_for_pod(db, store, pod.id, "per_listener")
    for generated in files:
        assert not set(generated.path) & set('<>:"\\|?*'), generated.path
        assert PSK.encode() not in generated.content
        assert STREAM.encode() not in generated.content


# --- A Deployment bundle ----------------------------------------------------------


def test_a_deployment_bundle_mixes_modes_pod_by_pod(env):
    factory, store = env
    with factory() as db:
        deployment = db.scalars(select(Deployment).order_by(Deployment.slug)).first()
        pods = [p for p in _pods(db) if p.deployment_id == deployment.id]
        assert len(pods) >= 2, "the demo deployment should hold several Pods"
        first, second = pods[0], pods[1]
        files = generate_for_deployment(
            db,
            store,
            deployment.id,
            {first.id: ("per_pod", frozenset()), second.id: ("per_listener", frozenset())},
        )
        expected = {pod_file_path(first.id)} | {
            listener_file_path(second.id, mac) for mac in _macs(db, second)
        }
    assert {f.path for f in files} == expected


def test_a_deployment_bundle_cannot_reach_into_another_deployment(env):
    factory, store = env
    with factory() as db:
        deployments = list(db.scalars(select(Deployment).order_by(Deployment.slug)))
        assert len(deployments) >= 2
        mine, theirs = deployments[0], deployments[1]
        foreign = next(p for p in _pods(db) if p.deployment_id == theirs.id)
        with pytest.raises(GenerationError, match=str(foreign.id)):
            generate_for_deployment(db, store, mine.id, {foreign.id: ("per_pod", frozenset())})
