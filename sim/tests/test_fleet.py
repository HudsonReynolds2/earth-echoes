"""SIM.4 acceptance: one command, a whole fleet.

The phase document's acceptance for this task is two claims plus a measured run:

* **one command brings a fleet up against the dev stack and every Aggregator
  reaches `applied`** — asserted here by calling `fleet.main()` with an argument
  list and nothing else, against a platform that is a real HTTP server with the
  publisher and the reconciliation consumer running inside it. No test drives a
  device, publishes a revision, or moves a revision along;
* **shutdown is clean, publishing an explicit `offline` rather than leaning on
  the LWT** — asserted by the ORDER of the two status messages the platform
  recorded, because a will is composed at CONNECT time and is therefore older
  than every `online` that followed it. An `offline` newer than the `online` can
  only have been published by the device on its way out;
* the 20 × 30 run itself is a manual load check, not a gate test, and its
  measured numbers live in `guide/sim-verification.md` (phase doc SIM.4/SIM.5).

The stagger, the counters and every refusal that has to happen at STARTUP are
asserted beside those, because a fleet runner whose failures only appear at hour
two of a load run is the thing this task exists not to ship.

Everything here is provisioned over the REST API and credentialled by running
the platform's own generator, in that order — which is the only order broker
accounts can be minted in, since they come from inventory. The consequence is
`Broker.refresh()`: material reaches the container by `docker cp`, so a
mid-run devbroker re-run has to be shipped in before Mosquitto is told to
re-read it.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from app.models import AggregatorStatus, ConfigRevision, DeviceState, Listener, Pod
from conftest import (
    BACKEND,
    DEP,
    OWNER,
    PASSWORD,
    Platform,
    aggregator_id_of,
    mint_broker_credentials,
    uvicorn_server,
)
from sqlalchemy import select

import fleet as runner
from fleet import Fleet, build_fleet, choose_scenario, device_keys
from provision import (
    DEVBROKER_COMMAND,
    FleetPlan,
    Operator,
    ProvisionedAggregator,
    ProvisionedFleet,
    ProvisionError,
    broker_endpoint,
    device_login,
    provision_hierarchy,
)
from scenarios import ScenarioError, load_scenarios

#: The CI fleet the phase document names: **2 × 3**. Small enough that the whole
#: module pays for one set of containers and eight revisions, large enough to be
#: a fleet rather than a device — two Aggregators prove the stagger and the
#: per-device counters, three Listeners each prove the parent reports on their
#: behalf and that the local link carries a revision per child.
#:
#: Written here rather than read from `EOE_SIM_AGGREGATORS`: scale is a parameter
#: everywhere in the harness (phase SIM section 2), but a GATE whose size came
#: from the environment would run one fleet locally and another in CI, and R0's
#: counts would stop meaning the same thing in the two places. 20 × 30 is the
#: documented load check and lives in the walkthrough.
AGGREGATORS = 2
LISTENERS = 3

#: How long the platform is given to move a revision after the device answered.
#: The device's own convergence is waited on by `fleet.py`; this is only the
#: consumer's lag behind a report that is already at the broker.
SETTLE = 120.0


@dataclass(frozen=True)
class Provisioned:
    """A fleet that exists in inventory and has broker accounts."""

    plan: FleetPlan
    fleet: ProvisionedFleet
    manifest: dict[str, Any]
    platform: Platform
    base_url: str

    def session(self) -> Operator:
        """A freshly logged-in operator against the CURRENT server.

        Fresh, and not the one that provisioned the hierarchy: that session
        belonged to the server the credential rotation forced down, and a stale
        cookie jar pointed at a closed port fails as "cannot reach the platform"
        three tests away from the reason.
        """
        operator = Operator(self.base_url)
        operator.login(OWNER, PASSWORD)
        return operator

    def argv(self, *extra: str) -> list[str]:
        """The command line that brings THIS fleet up. Built here so the
        acceptance test cannot drift from the fleet the fixture provisioned —
        a runner pointed at a differently-shaped fleet fails at connect with a
        credential error and a very long detour."""
        return [
            "--aggregators",
            str(self.plan.aggregators),
            "--listeners-per-aggregator",
            str(self.plan.listeners_per_aggregator),
            "--deployment",
            self.plan.deployment_slug,
            "--api",
            self.base_url,
            "--operator",
            OWNER,
            "--certs",
            str(self.platform.certs),
            "--broker",
            f"localhost:{self.platform.broker.port}",
            *extra,
        ]


@pytest.fixture(scope="module")
def provisioned(live_stack: Platform):
    """A provisioned, credentialled fleet — in the order reality imposes.

    This fixture is the operator's real sequence, performed rather than worked
    around, and every step of it is forced by something:

    1. **Inventory first, over REST**, because broker accounts are minted FROM
       inventory. An Aggregator that does not exist yet cannot have a credential.
    2. **Credentials second**, by running the platform's own generator. This
       rotates EVERY password, including the platform's own — the generator
       rewrites the password file and the SecretStore entries in one pass so they
       cannot drift apart (E3.1).
    3. **The material shipped into the live broker**, because it got there by
       `docker cp` and not a bind mount: a SIGHUP alone re-reads the OLD accounts
       and refuses every new one with a bare "not authorised".
    4. **The API restarted**, because step 2 just invalidated the credential its
       live broker sessions were holding, and `MqttClientManager.start()` reads
       its coordinates exactly once (E3.2, deliberately: the worker owns the
       lifecycle and can restart it). Without this the fleet connects perfectly
       and every revision stays `draft`, because the platform has no connection
       to publish over.

    Step 4 is `docker compose restart api worker` for an operator, and
    `guide/sim-verification.md` says so.
    """
    plan = FleetPlan(
        aggregators=AGGREGATORS, listeners_per_aggregator=LISTENERS, deployment_slug=DEP
    )
    with uvicorn_server(live_stack.app) as provisioning_url:
        operator = Operator(provisioning_url)
        operator.login(OWNER, PASSWORD)
        fleet = provision_hierarchy(operator, plan)
    manifest = mint_broker_credentials(
        live_stack.database_url,
        live_stack.kek,
        live_stack.certs,
        host="localhost",
        port=live_stack.broker.port,
    )
    live_stack.broker.refresh()
    with uvicorn_server(live_stack.app) as base_url:
        yield Provisioned(
            plan=plan,
            fleet=fleet,
            manifest=manifest,
            platform=live_stack,
            base_url=base_url,
        )


# ---------------------------------------------------------------------------
# Reading the platform's mind, synchronously
# ---------------------------------------------------------------------------


def until(probe, *, what: str, timeout: float = SETTLE, interval: float = 0.25):
    """Poll `probe` until it returns something truthy.

    The synchronous twin of `conftest.eventually`, and deliberately its own
    function: the acceptance test is NOT async, because `fleet.main()` owns its
    event loop exactly as it does for an operator at a shell. A test that had to
    be async to call it would be asserting something else.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = probe()
        if found:
            return found
        assert time.monotonic() < deadline, f"the platform never {what} within {timeout:g}s"
        time.sleep(interval)


def fleet_revision_states(provisioned: Provisioned) -> list[str]:
    """The state of every revision cut for a device in this fleet.

    Every device: the Aggregators AND their Listeners, because an
    Aggregator-level change moves the Listeners' effective config too and a
    fleet whose Listener revisions never converged has not converged.
    """
    targets = {aggregator.aggregator_id for aggregator in provisioned.fleet.aggregators}
    targets |= {mac for aggregator in provisioned.fleet.aggregators for mac in aggregator.macs}
    with provisioned.platform.factory() as db:
        rows = db.execute(
            select(ConfigRevision.target_id, ConfigRevision.state).where(
                ConfigRevision.target_id.in_(targets)
            )
        ).all()
    return [row.state for row in rows]


def status_of(provisioned: Provisioned, aggregator_uuid: str) -> AggregatorStatus | None:
    with provisioned.platform.factory() as db:
        return db.scalars(
            select(AggregatorStatus).where(
                AggregatorStatus.aggregator_id
                == aggregator_id_of(provisioned.platform.factory, aggregator_uuid)
            )
        ).first()


def verdict(provisioned: Provisioned, aggregator_uuid: str, *, online: bool) -> Callable[[], Any]:
    """A probe for "the platform believes this device is up / down".

    A named function rather than a lambda inside `until` because the row has to
    be fetched before it can be tested, and a conditional expression evaluates
    its condition first — the walrus version of this reads well and is wrong.
    """

    def probe() -> AggregatorStatus | None:
        row = status_of(provisioned, aggregator_uuid)
        return row if row is not None and row.online is online else None

    return probe


def revisions_cut(provisioned: Provisioned, expected: int) -> Callable[[], Any]:
    def probe() -> list[str] | None:
        states = fleet_revision_states(provisioned)
        return states if len(states) >= expected else None

    return probe


def all_applied(provisioned: Provisioned) -> Callable[[], Any]:
    def probe() -> list[str] | None:
        states = fleet_revision_states(provisioned)
        return states if states and all(state == "applied" for state in states) else None

    return probe


# ---------------------------------------------------------------------------
# The plan: identities computed, not remembered
# ---------------------------------------------------------------------------


def test_a_fleet_plan_computes_unique_identities_for_every_device():
    """Every name and MAC is a pure function of the slug and two indices.

    Which is what makes provisioning idempotent AND re-attachable: a second
    command computes the same identities, finds them, and creates nothing. A
    plan that generated anything random could not be run twice.
    """
    plan = FleetPlan(aggregators=20, listeners_per_aggregator=30, deployment_slug="sim-fleet")
    assert plan.listeners == 600
    uuids = [plan.aggregator_uuid(index) for index in plan.indices()]
    macs = [mac for index in plan.indices() for mac in plan.macs(index)]
    names = [
        plan.listener_name(index, listener)
        for index in plan.indices()
        for listener in range(plan.listeners_per_aggregator)
    ]
    assert len(set(uuids)) == 20
    assert len(set(macs)) == 600, "two simulated Listeners share a MAC, which is the inventory PK"
    assert len(set(names)) == 600, "two Listeners share a name, unique within a deployment (E1.4)"
    assert len(set(plan.pod_name(index) for index in plan.indices())) == 20
    assert FleetPlan(deployment_slug="x") == FleetPlan(deployment_slug="x")


def test_two_fleets_in_two_deployments_never_claim_the_same_macs():
    """`listener.mac` is the PRIMARY KEY and it is GLOBAL (E1.1, D31).

    Two fleets of the same shape in two deployments would therefore collide on
    every single address if the prefix were a constant — the second one's import
    conflicts on row 1, with a message about hardware neither of them has. The
    prefix is derived from the deployment slug so that cannot happen, and it is
    derived rather than allocated because a plan has to be computable from the
    slug alone.
    """
    one = FleetPlan(aggregators=4, listeners_per_aggregator=4, deployment_slug="sim-fleet")
    other = FleetPlan(aggregators=4, listeners_per_aggregator=4, deployment_slug="sim-second")
    mine = {mac for index in one.indices() for mac in one.macs(index)}
    theirs = {mac for index in other.indices() for mac in other.macs(index)}
    assert one.mac_head != other.mac_head
    assert not mine & theirs, f"two deployments share MACs: {sorted(mine & theirs)[:4]}"
    # Same slug, same addresses, run after run: that IS the idempotence.
    assert FleetPlan(deployment_slug="sim-fleet").mac_head == one.mac_head
    # And an explicit prefix still wins, for the deployment whose derived one
    # ever collides with something real.
    assert (
        FleetPlan(deployment_slug="x", mac_prefix="02:51:4d:00")
        .macs(0)[0]
        .startswith("02:51:4D:00")
    )


def test_simulated_macs_stay_clear_of_the_demo_fixture():
    """E1.9's demo hierarchy owns `02:EE:0E:…`, and a fleet is routinely
    provisioned into a database that already holds it. Asserted for the slugs
    this repository actually uses, plus the refusal for the one-in-sixteen-
    million slug whose digest lands there anyway — refused with the flag that
    fixes it, rather than discovered as a conflicting import."""
    for slug in ("sim-fleet", DEP, "high-desert", "sim-rest-check"):
        plan = FleetPlan(aggregators=8, listeners_per_aggregator=8, deployment_slug=slug)
        assert not plan.mac_head.startswith("02:EE:0E"), slug
    with pytest.raises(ProvisionError, match="collides with the E1.9 demo fixture"):
        FleetPlan(deployment_slug="collides", mac_prefix="02:ee:0e:01")


def test_a_fleet_larger_than_the_mac_layout_is_refused_at_plan_time():
    """Refused before anything is created, rather than by a CHECK constraint on
    row 256 with a quarter of the fleet already in the database."""
    with pytest.raises(ProvisionError, match="exceeds this MAC layout"):
        FleetPlan(aggregators=300)
    with pytest.raises(ProvisionError, match="exceeds this MAC layout"):
        FleetPlan(listeners_per_aggregator=300)
    with pytest.raises(ProvisionError, match="not a fleet"):
        FleetPlan(aggregators=0)


def test_only_the_devices_a_revision_reached_are_waited_for():
    """The defect that turned a 20 × 30 load run into a 30-minute timeout.

    `POST /config/apply` cuts a revision only for a device whose EFFECTIVE config
    changed, so re-running the fleet with the same value is a legitimate no-op —
    E2 publishes nothing, correctly. A runner that then waited for all 620
    devices sat until its timeout waiting to be told something nobody was going
    to say, and reported it as a platform failure.

    `device_keys` is the translation that makes waiting precise, and it has to
    cross an identity boundary to do it: a revision addresses an Aggregator by
    its platform `id`, while a device knows itself by the `aggregator_uuid` in
    its topics (spec 4.2 — three identity columns, never conflated).
    """
    fleet = ProvisionedFleet(
        deployment_id="d",
        deployment_slug="sim-fleet",
        aggregators=(
            ProvisionedAggregator(
                index=0,
                aggregator_uuid="sim-fleet-sim-000",
                aggregator_id="platform-id-0",
                pod_id="p0",
                macs=("02:AA:BB:CC:00:00", "02:AA:BB:CC:00:01"),
            ),
        ),
    )
    told = device_keys(
        [
            {"target_type": "aggregator", "target_id": "platform-id-0"},
            {"target_type": "listener", "target_id": "02:AA:BB:CC:00:01"},
            # Real, and deliberately not this fleet's: an apply can reach a
            # deployment the runner does not hold every device of, and waiting
            # for one of those would hang forever.
            {"target_type": "aggregator", "target_id": "somebody-elses-id"},
            {"target_type": "listener", "target_id": "02:EE:0E:01:01:01"},
        ],
        fleet,
    )
    assert told == {"sim-fleet-sim-000", "02:AA:BB:CC:00:01"}
    assert device_keys([], fleet) == set(), "an apply that changed nothing tells nobody"


def test_a_fleet_with_nothing_published_to_it_does_not_wait():
    """The other half of the same defect: waiting for an empty set returns at
    once rather than blocking on a device that will never hear anything."""
    empty = Fleet([])
    assert asyncio.run(empty.wait_for_applies(timeout=1.0, only=set())) == 0.0


def test_a_broker_endpoint_without_a_port_takes_the_default():
    assert broker_endpoint("mosquitto:8883", default_port=1) == ("mosquitto", 8883)
    assert broker_endpoint("localhost", default_port=18883) == ("localhost", 18883)
    with pytest.raises(ProvisionError, match="is not a port"):
        broker_endpoint("localhost:mqtt", default_port=8883)


def test_a_missing_broker_account_names_the_step_that_was_skipped():
    """Accounts are minted from INVENTORY, so an Aggregator provisioned after
    the last generator run has none — and the fix is two commands, both of which
    the message has to name. The alternative is a TLS connection refused with
    "not authorised", which points at nothing."""
    with pytest.raises(ProvisionError, match="docker compose restart mosquitto"):
        device_login({"accounts": []}, "sim-fleet-sim-000", host="h", port=1, ca_cert=None)


# ---------------------------------------------------------------------------
# Startup refusals: everything wrong fails in the first second
# ---------------------------------------------------------------------------


def test_the_two_identity_scenarios_are_refused_before_anything_connects():
    """ACCEPTANCE-adjacent (D113): a fleet provisions every device it runs, so
    neither "a device inventory has never heard of" nor "a device claiming
    another parent's MAC" can be a fleet member. Offering them would bring
    twenty Aggregators up and prove nothing, so they are refused at startup with
    the reason and a pointer to the suite that does stage them."""
    scenarios = load_scenarios()
    for name in ("duplicate_mac", "unprovisioned_aggregator"):
        with pytest.raises(ScenarioError, match="cannot run against a provisioned fleet"):
            choose_scenario(scenarios, name)
    for name in ("apply_error", "drift", "disconnect", "missed_wake_window"):
        assert choose_scenario(scenarios, name) is not None, f"{name} should be runnable on a fleet"
    assert choose_scenario(scenarios, None) is None


def test_an_unknown_scenario_name_lists_the_ones_that_exist():
    with pytest.raises(ScenarioError, match="is not a shipped scenario"):
        choose_scenario(load_scenarios(), "drft")


def test_the_catalogue_names_every_scenario_and_every_behaviour(capsys):
    """`--list-scenarios` is the interface a later epic uses to find out what it
    can ask for without reading the code, so it has to be complete: every file
    AND every registered behaviour, with the suite-only ones marked."""
    assert runner.main(["--list-scenarios"]) == 0
    printed = capsys.readouterr().out
    for name in load_scenarios():
        assert name in printed
    assert "suite only" in printed, "the two suite-only scenarios must say so"
    assert "behaviours a scenario file may name" in printed


def test_a_run_with_no_certificates_names_the_command_that_makes_them(tmp_path, capsys):
    assert runner.main(["--certs", str(tmp_path), "--no-provision", "--no-apply"]) == 1
    assert "--certs-only" in capsys.readouterr().err


def test_no_provision_without_no_apply_is_refused_rather_than_half_run(tmp_path, capsys):
    """`--no-provision` makes no REST calls at all, so there are no inventory
    ids to publish a revision against. Refused up front instead of posting an
    apply whose selection is a list of empty strings — which the API would
    answer with a 422 about a UUID, three layers away from the flag that caused
    it."""
    (tmp_path / "ca.crt").write_text("not a certificate, and never read", encoding="utf-8")
    assert runner.main(["--certs", str(tmp_path), "--no-provision"]) == 1
    assert "--no-apply" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Provisioning over the platform's own API
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_provisioning_over_the_rest_api_is_idempotent(provisioned):
    """Run twice, and the second run creates nothing.

    Idempotent by ASKING what exists rather than by swallowing conflicts, which
    is why this test also asserts the ids are the same objects: a second run
    that quietly created a parallel hierarchy under suffixed names would also
    "succeed", and the fleet would then be twice the size it was asked for with
    half of it credentialled.
    """
    plan = FleetPlan(aggregators=2, listeners_per_aggregator=2, deployment_slug="sim-rest-check")
    operator = provisioned.session()

    first = provision_hierarchy(operator, plan)
    assert len(first.aggregators) == 2
    assert first.listeners == 4
    assert all(aggregator.macs for aggregator in first.aggregators)

    second = provision_hierarchy(operator, plan)
    assert second == first, "a second run created new rows instead of finding the old ones"

    factory = provisioned.platform.factory
    with factory() as db:
        pods = db.scalars(select(Pod).where(Pod.name.like("SIM Pod %"))).all()
        listeners = db.scalars(select(Listener).where(Listener.name.like("sim-%"))).all()
    assert len([pod for pod in pods if str(pod.deployment_id) == first.deployment_id]) == 2
    assert len([row for row in listeners if str(row.deployment_id) == first.deployment_id]) == 4
    assert all("sim" in row.tags for row in listeners), (
        "imported Listeners are tagged, which is how an operator tells simulated "
        "hardware from the real thing in their own inventory"
    )


@pytest.mark.integration
def test_a_listener_import_that_could_not_commit_is_a_failure_not_a_shrug(provisioned):
    """E1.6 is all-or-nothing by default and reports row errors as DATA, with a
    200. A provisioner that read `committed: false` as success would leave a
    fleet silently short of Listeners with a green log — so the report is
    checked, and the first few row errors ride the exception."""
    report = provisioned.session().post(
        "/listeners/import",
        {"rows": [{"mac": "not-a-mac", "name": "x", "aggregator_uuid": "nobody"}]},
    )
    assert report["committed"] is False and report["failed"] == 1
    assert report["rows"][0]["status"] == "error"


# ---------------------------------------------------------------------------
# ACCEPTANCE
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.timeout(600)
def test_one_command_brings_a_fleet_up_and_every_aggregator_reaches_applied(
    provisioned, monkeypatch, capsys
):
    """ACCEPTANCE (phase doc SIM.4), the first claim.

    One call, with an argument list and an environment variable, and nothing in
    this test touches a device or the wire. `fleet.py` provisions (idempotently,
    over REST), connects a staggered fleet on per-device credentials, publishes
    an Aggregator-level revision as an operator, and waits for every Aggregator
    to converge; the platform's own worker moves each revision to `applied`
    because the checksums the devices computed matched.

    Both halves of the fleet are asserted: the Aggregators AND their Listeners,
    because the revision the fleet publishes changes the Listeners' effective
    config too, and a Listener revision that never converged means the local
    link never carried anything.
    """
    monkeypatch.setenv("EOE_SIM_PASSWORD", PASSWORD)
    expected = provisioned.plan.aggregators * (1 + provisioned.plan.listeners_per_aggregator)

    assert runner.main(provisioned.argv("--apply-timeout", "300")) == 0

    printed = capsys.readouterr().out
    assert "time-to-all-applied" in printed, "the run has to report the figure SIM.4 asks for"
    assert "peak resident set size" in printed

    states = until(
        revisions_cut(provisioned, expected), what=f"cut {expected} revisions for the fleet"
    )
    assert len(states) == expected, f"expected one revision per device, got {states}"
    until(all_applied(provisioned), what="moved every fleet revision to applied")

    with provisioned.platform.factory() as db:
        macs = {mac for aggregator in provisioned.fleet.aggregators for mac in aggregator.macs}
        reported = db.scalars(
            select(DeviceState).where(
                DeviceState.entity_type == "listener", DeviceState.entity_id.in_(macs)
            )
        ).all()
    assert len(reported) == len(macs), (
        "the platform holds reported state for fewer Listeners than the fleet has; "
        "every Listener is reported on by its parent (spec 6.4) and there is no other way in"
    )


@pytest.mark.integration
@pytest.mark.timeout(600)
def test_the_fleet_staggers_its_connects_counts_what_it_did_and_says_goodbye(provisioned):
    """ACCEPTANCE (phase doc SIM.4), the second and third claims.

    **Staggered**, asserted as a lower bound on elapsed time, which cannot flake
    in the direction that matters: a loaded host makes the fleet slower, never
    simultaneous.

    **Counters**, read off the devices themselves so they cannot drift from
    behaviour.

    **A clean shutdown that SAYS so**, asserted by the order of the two status
    messages. A will is composed at CONNECT time, so an LWT `offline` carries a
    timestamp OLDER than the `online` that followed it (E3.8 is careful never to
    order the status topic by the payload clock). An `offline` newer than the
    `online` can therefore only have been published by the device on its way
    out — which is the difference between a harness that can test a crash and
    one whose every exit looks like one.
    """
    stagger = 0.4
    fleet = build_fleet(
        provisioned.fleet,
        provisioned.manifest,
        host="localhost",
        port=provisioned.platform.broker.port,
        ca_cert=provisioned.platform.certs / "ca.crt",
        stagger=stagger,
    )
    started = time.monotonic()
    asyncio.run(_bring_up_and_down(fleet, provisioned))
    assert time.monotonic() - started >= stagger * (len(fleet.devices) - 1), (
        "the fleet connected faster than its own stagger allows, so start-up was not staggered"
    )


async def _bring_up_and_down(fleet: Fleet, provisioned: Provisioned) -> None:
    """The body of the test above, in the loop `Fleet` needs.

    Its own function rather than an async test because the assertions about the
    platform are synchronous database reads and the ones about the fleet are
    not; keeping the loop this narrow means nothing here can accidentally wait
    on a device while holding a session open.
    """
    online: dict[str, Any] = {}
    try:
        await fleet.connect()
        counters = fleet.counters()
        assert counters.connected == counters.devices == provisioned.plan.aggregators
        assert counters.listeners == provisioned.fleet.listeners
        assert counters.publishes >= counters.devices, "every device announces itself, retained"
        # The revisions from the acceptance test above are still retained, so a
        # fleet that reconnects converges again with nobody republishing —
        # spec 6.4's whole point, and the reason these counters are non-zero.
        # `wait_for_applies` covers the Listeners as well as their parents, which
        # is what makes the two assertions below a fact rather than a race.
        await fleet.wait_for_applies(timeout=120.0)
        applied = fleet.counters()
        assert applied.aggregator_applies >= counters.devices
        assert applied.listener_applies >= applied.listeners
        assert applied.reports >= applied.applies, "an apply that was not reported is invisible"
        for aggregator in provisioned.fleet.aggregators:
            row = until(
                verdict(provisioned, aggregator.aggregator_uuid, online=True),
                what=f"recorded {aggregator.aggregator_uuid} online",
            )
            online[aggregator.aggregator_uuid] = row.declared_at
    finally:
        await fleet.shutdown()

    assert all(not device.connected for device in fleet.devices)
    for aggregator in provisioned.fleet.aggregators:
        row = until(
            verdict(provisioned, aggregator.aggregator_uuid, online=False),
            what=f"recorded {aggregator.aggregator_uuid} offline",
        )
        assert row.declared_at > online[aggregator.aggregator_uuid], (
            "the offline the platform recorded is OLDER than the online before it, which "
            "means it was the will the broker had been holding since CONNECT — this fleet "
            "died rather than shutting down, and a harness whose only exit looks like a "
            "crash cannot be used to test a crash"
        )


# ---------------------------------------------------------------------------
# The container, and the paths it depends on
# ---------------------------------------------------------------------------


def test_the_sim_image_carries_the_published_contract_and_no_other_platform_code():
    """The harness boundary, asserted against the Dockerfile.

    `test_harness_boundaries` proves no `/sim` module imports anything but
    `app.contracts` from the platform. This proves the container could not: only
    the contract package is copied in, so an import of `app.models` there fails
    on a missing file rather than on a rule somebody could delete. The
    Dockerfile is read rather than built, because building it is `docker`'s job
    and this is a claim about what it says.
    """
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    platform_copies = [
        line
        for line in text.splitlines()
        if line.startswith("COPY") and "backend/" in line and "--from" not in line
    ]
    assert platform_copies, "the image copies no platform code at all; the contract is required"
    assert all("app/contracts" in line or "app/__init__.py" in line for line in platform_copies), (
        f"the sim image copies platform code beyond the published contract: {platform_copies}"
    )
    assert "--no-dev" in text, "the image must install a DEVICE's dependency set, not the suite's"


def test_the_harness_runs_the_platforms_own_credential_generator():
    """`mint_credentials` invokes `app.devbroker` and never reimplements it.

    Asserted on the DEFAULT command, because that is the one an operator gets:
    the suite overrides it with this interpreter (there is no uv inside a pytest
    process and no network to fetch one with), so a default that had quietly
    become something else would be exercised by nobody.
    """
    assert DEVBROKER_COMMAND[-2:] == ("-m", "app.devbroker")
    assert "uv" in DEVBROKER_COMMAND, "the documented operator command is `uv run`"
    assert (BACKEND / "app" / "devbroker.py").is_file(), (
        "the generator this points at does not exist; broker accounts would be minted by "
        "nothing, or worse, by something written here"
    )
