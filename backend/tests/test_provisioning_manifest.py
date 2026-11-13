"""E4.7: the bundle manifest and README. **TEST-CRITICAL (spec 14.5).**

**This suite is locked documentation under rule R0** from the commit that
introduces it (gate 75). Later sessions extend it and never weaken it, and any
change to it is recorded in `docs/DECISIONS.md` (D177). It is the fourth of
the four suites `.claude/rules/project-rules.json` names.

Why the manifest is test-critical: it is the only artifact that says what
SHOULD be on a card. A bundle whose manifest disagrees with its own contents
is undetectable in the field.

The suite is **table-driven over every file kind and every generation mode**
(`CASES`). Each case builds a real bundle from a seeded database and asserts:

1. **Completeness, both directions.** The listed paths are exactly the archive
   members minus `manifest.json` (the one named exclusion), and every sha256
   matches its member's bytes.
2. **Every device has exactly one card.** Each Listener in each Pod and each
   Pod's Aggregator appears in exactly one entry's `cards`.
3. **Each entry says what is on the card** (spec 8.3): the kind, the pod, the
   generating revision per card, and expected contents equal to the file
   itself with secret values replaced by markers.
4. **No secret value anywhere** in the manifest or README.
5. **The README names every file and carries the DRAFT and plaintext notes**,
   plus the missing-credentials warning when the flag is off.

`verify_members` is the production check E4.8 runs on every archive. Its
negative tests show it catches a tampered, missing or stray member. Without
those, a checker that always returned [] would pass every positive test here.
"""

import json
import uuid
from dataclasses import dataclass

import pytest
import yaml
from conftest import ephemeral_postgres, make_kek
from sqlalchemy import select, update

from app.config.overrides import put_overrides
from app.db import create_session_factory
from app.devbroker import plan_accounts, register_services
from app.models import Aggregator, Deployment, Listener, Pod
from app.provisioning.bootstrap import generate_settings_yaml
from app.provisioning.envelope import FirmwareKey
from app.provisioning.generate import generate_for_pod
from app.provisioning.manifest import (
    MANIFEST_PATH,
    PLAINTEXT_MARKER,
    README_PATH,
    SEALED_MARKER,
    BundleMeta,
    bundle_members,
    sha256,
    verify_members,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy
from app.services.credentials import DeviceCredential

pytestmark = pytest.mark.integration

PSK = "e47-planted-psk-61d0aa-never-in-a-manifest"
STREAM = "e47-planted-stream-0c9b3e-never-in-a-manifest"
DEVICE_LOGIN_MARKER = "e47-planted-device-pw-7a21f0"
FIRMWARE = FirmwareKey.from_base64(make_kek(), key_version=2)
CA = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"

NETWORK = {
    "network.wifi_ssid": "e47-halow",
    "network.wifi_password": PSK,
    "network.stream_key": STREAM,
    "network.aggregator_ip": "10.47.0.1",
    "network.stream_endpoint": "10.47.0.1:8765/stream",
}


class FakeProvider:
    async def mint(self, coordinates, aggregator_uuid: str) -> DeviceCredential:
        return DeviceCredential(username=f"agg-{aggregator_uuid}", password=DEVICE_LOGIN_MARKER)

    async def revoke(self, coordinates, aggregator_uuid: str) -> None:  # pragma: no cover
        raise AssertionError("never revoked here")


@dataclass(frozen=True)
class Case:
    """One bundle shape. `modes` is per Pod index: (mode, how many of that
    Pod's Listeners to name)."""

    name: str
    modes: tuple[tuple[str, int], ...]
    credentials: bool
    kinds: frozenset[str]


#: Every file kind, and every generation mode, at least once, plus a
#: Deployment bundle mixing all three and both credential flag states.
CASES = [
    Case("per_pod", (("per_pod", 0),), True, frozenset({"pod_file", "settings_yaml"})),
    Case(
        "per_listener", (("per_listener", 0),), True, frozenset({"listener_file", "settings_yaml"})
    ),
    Case("mixed", (("mixed", 2),), True, frozenset({"pod_file", "listener_file", "settings_yaml"})),
    Case(
        "deployment_all_modes",
        (("per_pod", 0), ("per_listener", 0), ("mixed", 1)),
        True,
        frozenset({"pod_file", "listener_file", "settings_yaml"}),
    ),
    Case(
        "credentials_off",
        (("mixed", 1),),
        False,
        frozenset({"pod_file", "listener_file", "settings_yaml"}),
    ),
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def env():
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        store = SecretStore(factory, make_kek())
        with factory() as db:
            seed_demo_hierarchy(db)
            for pod in db.scalars(select(Pod)):
                put_overrides(db, store, "pod", str(pod.id), NETWORK)
            db.commit()
            register_services(
                db, store, plan_accounts(db), host="broker.example", port=8883, ca_cert_pem=CA
            )
            db.execute(update(Deployment).values(services_status="verified"))
            db.commit()
        yield factory, store


async def _build(factory, store, case: Case):
    """A real bundle for `case`, from the first Deployment that has enough Pods."""
    with factory() as db:
        deployment = next(
            d
            for d in db.scalars(select(Deployment).order_by(Deployment.slug))
            if len(_pods(db, d.id)) >= len(case.modes)
        )
        pods = _pods(db, deployment.id)[: len(case.modes)]
        files, plan, expected_devices = [], {}, set()
        for pod, (mode, named) in zip(pods, case.modes, strict=True):
            macs = _macs(db, pod)
            chosen = frozenset(macs[:named])
            files += generate_for_pod(db, store, pod.id, mode, chosen, firmware_key=FIRMWARE)
            files.append(
                await generate_settings_yaml(
                    db, factory, store, FakeProvider(), pod.id, include_credentials=case.credentials
                )
            )
            plan[str(pod.id)] = {"mode": mode, "listener_macs": sorted(chosen)}
            aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod.id))
            expected_devices |= {("listener", mac) for mac in macs}
            expected_devices.add(("aggregator", str(aggregator.id)))
        db.commit()
        meta = BundleMeta(
            bundle_id=uuid.uuid4(),
            deployment_id=deployment.id,
            deployment_name=deployment.name,
            pod_names={pod.id: pod.name for pod in pods},
            plan=plan,
            template_version=1,
            firmware_key_version=FIRMWARE.key_version,
            bootstrap_credentials=case.credentials,
        )
    return meta, files, bundle_members(meta, files), expected_devices


def _pods(db, deployment_id) -> list[Pod]:
    return list(
        db.scalars(
            select(Pod)
            .join(Aggregator, Aggregator.pod_id == Pod.id)
            .where(Pod.deployment_id == deployment_id)
            .order_by(Pod.name)
        )
    )


def _macs(db, pod: Pod) -> list[str]:
    aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod.id))
    return list(
        db.scalars(
            select(Listener.mac)
            .where(Listener.aggregator_id == aggregator.id)
            .order_by(Listener.mac)
        )
    )


def _manifest(members) -> dict:
    return json.loads(members[MANIFEST_PATH])


# --- The table-driven contract ------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_completeness_in_both_directions(env, case):
    factory, store = env
    _, _, members, _ = await _build(factory, store, case)
    manifest = _manifest(members)

    listed = {entry["path"] for entry in manifest["files"]}
    assert manifest["excludes"] == [MANIFEST_PATH]
    # Every member is listed (bar the one named exclusion) ...
    assert set(members) - {MANIFEST_PATH} == listed
    # ... and every listed path is a member.
    assert listed <= set(members)
    for entry in manifest["files"]:
        assert entry["sha256"] == sha256(members[entry["path"]]), entry["path"]
    assert verify_members(members) == []


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_every_device_has_exactly_one_card_and_every_kind_is_present(env, case):
    factory, store = env
    _, _, members, expected_devices = await _build(factory, store, case)
    manifest = _manifest(members)

    cards = [
        (card["target_type"], card["target_id"])
        for entry in manifest["files"]
        for card in entry["cards"]
    ]
    assert len(cards) == len(set(cards)), "a device appears on two cards"
    assert set(cards) == expected_devices
    kinds = {entry["kind"] for entry in manifest["files"]} - {"readme"}
    assert kinds == case.kinds


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_each_entry_says_what_is_on_the_card(env, case):
    """Spec 8.3: target, checksum, generating revision, expected contents."""
    factory, store = env
    _, files, members, _ = await _build(factory, store, case)
    by_path = {entry["path"]: entry for entry in _manifest(members)["files"]}

    for generated in files:
        entry = by_path[generated.path]
        record_kinds = {r.file_kind for r in generated.records}
        assert {entry["kind"]} == record_kinds
        assert entry["pod"] == str(generated.records[0].pod_id)
        assert [
            (c["target_id"], c["revision_id"], c["revision_checksum"]) for c in entry["cards"]
        ] == [
            (
                r.target_id,
                str(r.config_revision_id) if r.config_revision_id else None,
                r.config_checksum,
            )
            for r in generated.records
        ]
        # Expected contents ARE the file, with secret values replaced.
        actual = yaml.safe_load(generated.content)
        expected = entry["expected_contents"]
        if entry["kind"] == "settings_yaml":
            if case.credentials:
                assert expected["mqtt"].pop("password") == PLAINTEXT_MARKER
                actual["mqtt"].pop("password")
            else:
                assert "password" not in expected["mqtt"]
        else:
            for name in ("wrapped_dek", "wifi_password", "stream_key"):
                assert expected["secrets"].pop(name) == SEALED_MARKER
                actual["secrets"].pop(name)
        assert expected == actual


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_no_secret_value_in_the_manifest_or_readme(env, case):
    factory, store = env
    _, _, members, _ = await _build(factory, store, case)
    for path in (MANIFEST_PATH, README_PATH):
        for secret in (PSK, STREAM, DEVICE_LOGIN_MARKER):
            assert secret.encode() not in members[path], f"{secret[:12]}… leaked into {path}"


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_the_readme_names_every_file_and_the_deliberate_choices(env, case):
    factory, store = env
    _, files, members, _ = await _build(factory, store, case)
    readme = members[README_PATH].decode()

    for generated in files:
        assert f"`{generated.path}`" in readme
    assert "DRAFT `device_config` v1, pending firmware sign-off" in readme
    assert "plaintext, by design" in readme
    warned = "ACTION NEEDED: broker credentials are absent" in readme
    assert warned is not case.credentials


@pytest.mark.anyio
async def test_the_manifest_is_deterministic_for_one_set_of_files(env):
    factory, store = env
    meta, files, members, _ = await _build(factory, store, CASES[2])
    assert bundle_members(meta, files) == members


# --- verify_members bites ------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_catches_a_tampered_member(env):
    factory, store = env
    _, _, members, _ = await _build(factory, store, CASES[0])
    path = next(p for p in members if p.endswith("device_config.yaml"))
    tampered = dict(members) | {path: members[path] + b"\n# edited by hand\n"}
    assert verify_members(tampered) == [f"checksum mismatch {path}"]


@pytest.mark.anyio
async def test_verify_catches_a_missing_and_a_stray_member(env):
    factory, store = env
    _, _, members, _ = await _build(factory, store, CASES[2])
    path = next(p for p in members if "/listeners/" in p)
    missing = {k: v for k, v in members.items() if k != path}
    assert verify_members(missing) == [f"listed but absent {path}"]
    stray = dict(members) | {"pods/extra.yaml": b"x"}
    assert verify_members(stray) == ["unlisted member pods/extra.yaml"]


def test_verify_requires_the_manifest_itself():
    assert verify_members({"README.md": b"x"}) == [f"{MANIFEST_PATH} is missing"]


def test_a_duplicate_path_is_refused_before_it_reaches_an_archive():
    from app.provisioning.generate import GeneratedFile, PlannedRecord

    pod = uuid.uuid4()
    record = PlannedRecord("listener", "02:00:00:00:00:01", pod, "agg", "pod_file", "p", None, None)
    files = [GeneratedFile("p", b"a", (record,)), GeneratedFile("p", b"b", (record,))]
    meta = BundleMeta(uuid.uuid4(), uuid.uuid4(), "d", {pod: "pod"}, {}, 1, 1, True)
    with pytest.raises(ValueError, match="duplicate archive path p"):
        bundle_members(meta, files)
