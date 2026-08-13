"""The fleet runner (task SIM.4).

One command brings a configurable fleet up against a real broker and a real
platform, waits for every Aggregator to converge, optionally makes some of them
misbehave, and takes the whole thing down politely:

    uv run python fleet.py                                   # 20 × 30, the documented scale
    uv run python fleet.py --aggregators 2 --listeners-per-aggregator 3
    uv run python fleet.py --scenario drift --scenario-devices 3 --stay
    uv run python fleet.py --list-scenarios

Four properties of this file are load-bearing rather than stylistic.

**A fleet is concurrency, not processes** (phase doc fixed choice). Every
Aggregator is a `MockAggregator` on one event loop; twenty of them, or six
hundred Listeners' worth, is the same process with more objects in it.

**Start-up is staggered.** Twenty simultaneous TLS handshakes against one
Mosquitto is a thundering herd nothing in the field produces — real Aggregators
come up when their power does — and a harness whose failures were all
connect-storm artefacts would waste an epic's worth of investigation. The
stagger is a parameter, so a connect storm stays something you can ask for on
purpose.

**Shutdown is clean, which means SAYING so.** Every device publishes an explicit
`offline` and sends a DISCONNECT, so the broker discards its will. A harness
whose only exit looked like a crash could not be used to test a crash — and
testing crashes is what `kill()` and the `disconnect` scenario are for.

**Scenarios load at STARTUP, before anything connects.** That is D107's whole
point: an unknown behaviour name or an out-of-range parameter must fail in the
second before a run, not at hour two of a load run with a fleet up and nothing
to show for it. `--scenario` is resolved in the same breath, including the two
scenarios a provisioned fleet structurally cannot host (D113).
"""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from device import MockAggregator, MockListener
from provision import (
    FLEET_SETTING,
    FleetPlan,
    Operator,
    ProvisionedAggregator,
    ProvisionedFleet,
    ProvisionError,
    add_fleet_arguments,
    apply_fleet_config,
    broker_endpoint,
    device_login,
    load_accounts,
    operator_from,
    plan_from,
    provision_hierarchy,
)
from scenarios import BEHAVIOURS, Scenario, ScenarioContext, ScenarioError, load_scenarios

log = logging.getLogger("fleet")

#: What a device on the HOST dials: the dev compose stack publishes Mosquitto's
#: 8883 on 18883 (PHASE0-2-02). Inside the compose network it is `mosquitto:8883`,
#: which is what the `sim` service's environment says.
DEFAULT_BROKER: Final = "localhost:18883"

#: Seconds between two devices' connects. Small enough that a 20-device fleet is
#: up in a second, large enough that the broker is never asked for twenty TLS
#: handshakes at one instant.
DEFAULT_STAGGER: Final = 0.05

#: How long a fleet waits for every Aggregator to reach `applied`. Generous
#: because the wait is dominated by the platform's per-revision publish and not
#: by the devices (see `guide/sim-verification.md`); a fleet that gave up early
#: would report a platform cost as a harness failure.
DEFAULT_APPLY_TIMEOUT: Final = 1800.0

#: The value the fleet publishes for `listener.wake_grace_seconds`. Deliberately
#: not spec 5.3's default of 30: a change that changed nothing is a no-op E2
#: declines to cut a revision for, and a fleet waiting for an apply that was
#: never going to happen is a confusing way to learn that.
FLEET_SETTING_VALUE: Final = 45


class Converging(Protocol):
    """What it takes to be a device this fleet waits for.

    Both halves of the fleet satisfy it — a `MockAggregator` and each of its
    `MockListener`s — which is the point: a Listener converges no less than its
    parent does, and a runner that could only wait for the things holding an
    MQTT session would return with half the fleet's revisions still in flight.
    A protocol rather than a union so the two objects stay unrelated: a Listener
    is emphatically not a kind of Aggregator (spec 6.4), and expressing this as
    inheritance would be the first step towards giving it a session.
    """

    applied_revision_ids: list[uuid.UUID]

    async def wait_for_apply(
        self, revision_id: uuid.UUID | None = None, *, timeout: float = 30.0
    ) -> uuid.UUID: ...


@dataclass(frozen=True)
class FleetCounters:
    """What the fleet did, counted by the DEVICES rather than by the runner.

    Every number here is read off the mock devices themselves (`connected`,
    `published_messages`, `applied_revision_ids`, `published_reports`,
    `commands_executed`), so a counter cannot drift from behaviour: there is no
    second place where a publish is tallied and therefore nowhere for the two to
    disagree.
    """

    devices: int
    connected: int
    listeners: int
    publishes: int
    aggregator_applies: int
    listener_applies: int
    reports: int
    commands: int

    @property
    def applies(self) -> int:
        return self.aggregator_applies + self.listener_applies

    def render(self) -> str:
        return (
            f"{self.connected}/{self.devices} Aggregators connected, "
            f"{self.listeners} Listeners, "
            f"{self.publishes} publishes, "
            f"{self.applies} applies ({self.aggregator_applies} aggregator / "
            f"{self.listener_applies} listener), "
            f"{self.reports} reports, {self.commands} commands"
        )


class Fleet:
    """A collection of mock Aggregators, brought up and down as one."""

    def __init__(
        self, devices: Sequence[MockAggregator], *, stagger: float = DEFAULT_STAGGER
    ) -> None:
        self.devices = list(devices)
        self.stagger = stagger
        #: When the last device finished connecting — the anchor for
        #: time-to-all-applied, because measuring from before the fleet existed
        #: would bill the stagger to the platform.
        self.connected_at: float | None = None

    def __str__(self) -> str:
        return f"fleet of {len(self.devices)} Aggregators"

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Connect every device, staggered, and fail loudly on the first refusal.

        Sequential rather than gathered: the stagger is the point, and a
        `gather` of twenty connects with sleeps inside them would still hand the
        broker whatever the scheduler felt like. A device that cannot connect
        aborts the run, because a fleet quietly three Aggregators short is a
        load test whose result means nothing.
        """
        started = time.monotonic()
        for index, device in enumerate(self.devices):
            if index and self.stagger:
                await asyncio.sleep(self.stagger)
            try:
                await device.connect()
            except Exception as error:
                raise RuntimeError(f"{device} could not connect: {error}") from error
        self.connected_at = time.monotonic()
        log.info(
            "%s connected in %.1fs (stagger %.3fs)", self, self.connected_at - started, self.stagger
        )

    async def shutdown(self) -> None:
        """Take every device down POLITELY, and never leave one up.

        Concurrent, because a polite shutdown of twenty Aggregators one at a
        time is twenty round trips of nothing happening, and because the
        ordering that mattered (subscribe before announce) was a connect-time
        property. Failures are logged and the sweep continues: one device that
        cannot say goodbye must not strand the other nineteen holding sessions.
        """
        results = await asyncio.gather(
            *(device.disconnect() for device in self.devices), return_exceptions=True
        )
        for device, result in zip(self.devices, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("%s did not shut down cleanly: %r", device, result)
        log.info("%s shut down", self)

    # --- convergence --------------------------------------------------------

    def expecting(self, only: Collection[str] | None = None) -> list[Converging]:
        """The devices this fleet waits for, by key (`aggregator_uuid` or MAC).

        `only=None` means every device it holds. **Passing a set matters**, and
        the reason is a defect this file shipped once: `POST /config/apply` cuts
        a revision only for a device whose effective config actually CHANGED, so
        running the runner twice with the same value is a legitimate no-op — E2
        publishes nothing the second time, correctly. A runner that then waited
        for all 620 devices would sit until its timeout waiting to be told
        something nobody was going to say, and report a platform failure. So the
        caller passes the devices a revision was genuinely published to.
        """
        waiters: list[Converging] = []
        for aggregator in self.devices:
            if only is None or aggregator.aggregator_uuid in only:
                waiters.append(aggregator)
            for listener in aggregator.listeners.values():
                if only is None or listener.mac in only:
                    waiters.append(listener)
        return waiters

    async def wait_for_applies(
        self, timeout: float = DEFAULT_APPLY_TIMEOUT, *, only: Collection[str] | None = None
    ) -> float:
        """Block until every device in `only` (default: all of them) has applied.

        Every device, which means the Listeners too and not only their parents.
        An Aggregator-level change moves its Listeners' effective config as well,
        so E2 cuts one revision per Listener and the platform publishes all of
        them — and a runner that returned as soon as the Aggregators had
        converged would shut the fleet down with those Listener applies still in
        flight. The revisions then sit `pending` until the reconciliation
        timeout, and the run reports success for a fleet that did not finish
        converging. Also a defect this file shipped once, found by the acceptance
        test asserting on the platform's rows rather than on the runner's word.

        Returns seconds since the fleet finished connecting — the
        time-to-all-applied figure the phase document asks to have recorded. The
        timeout message names how many are still waiting and one of them, so a
        failure is a place to look rather than a number.
        """
        waiters = self.expecting(only)
        if not waiters:
            log.info("no revision was published to this fleet; there is nothing to wait for")
            return 0.0
        anchor = self.connected_at or time.monotonic()
        deadline = anchor + timeout
        for waiter in waiters:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(self._never_applied(timeout, waiters))
            try:
                await waiter.wait_for_apply(timeout=remaining)
            except TimeoutError as error:
                raise TimeoutError(self._never_applied(timeout, waiters)) from error
        elapsed = time.monotonic() - anchor
        log.info(
            "all %d devices applied a revision %.1fs after the fleet was up", len(waiters), elapsed
        )
        return elapsed

    def _never_applied(self, timeout: float, waiters: Sequence[Converging]) -> str:
        waiting = [str(waiter) for waiter in waiters if not waiter.applied_revision_ids]
        return (
            f"{len(waiting)} of {len(waiters)} devices never applied a revision within "
            f"{timeout:g}s (for example {waiting[0] if waiting else 'none'}). A device with no "
            "revision published to it waits forever: check that the apply reached the broker "
            "and that EOE_PUBLISH_ENABLED is on."
        )

    # --- misbehaviour -------------------------------------------------------

    async def run_scenario(self, scenario: Scenario, *, devices: int = 0) -> list[MockAggregator]:
        """Run one scenario against the first `devices` members (0 = all).

        The subset exists because a fleet in which every device drifts is not a
        picture of anything: what E6 and E8 need is twenty devices of which
        three are wrong, so the wrong one has to be findable among healthy ones.
        First-N rather than a random sample, so a run is reproducible and the
        affected devices are named in the log rather than discovered.
        """
        chosen = self.devices[: devices or len(self.devices)]
        log.info(
            "running scenario %s against %d of %d Aggregators: %s",
            scenario.name,
            len(chosen),
            len(self.devices),
            scenario.expects,
        )
        for device in chosen:
            await scenario.run(ScenarioContext(aggregator=device))
        return chosen

    # --- reporting ----------------------------------------------------------

    def counters(self) -> FleetCounters:
        listeners = [listener for device in self.devices for listener in device.listeners.values()]
        return FleetCounters(
            devices=len(self.devices),
            connected=sum(1 for device in self.devices if device.connected),
            listeners=len(listeners),
            publishes=sum(device.published_messages for device in self.devices),
            aggregator_applies=sum(len(device.applied_revision_ids) for device in self.devices),
            listener_applies=sum(len(listener.applied_revision_ids) for listener in listeners),
            reports=(
                sum(device.published_reports for device in self.devices)
                + sum(listener.published_reports for listener in listeners)
            ),
            commands=sum(len(device.commands_executed) for device in self.devices),
        )


# ---------------------------------------------------------------------------
# Building one from a provisioned fleet
# ---------------------------------------------------------------------------


def build_fleet(
    provisioned: ProvisionedFleet,
    manifest: dict[str, Any],
    *,
    host: str,
    port: int,
    ca_cert: Path | None,
    stagger: float = DEFAULT_STAGGER,
) -> Fleet:
    """Turn inventory plus a credential manifest into unconnected devices.

    Listeners are attached BEFORE connect, so each device subscribes to its
    Listeners' desired subtopics in the same batch as its own (SIM.2) and a
    retained Listener revision arrives the instant the subscription lands
    rather than after a second round trip. `MockListener` is constructed
    directly rather than through `add_listener` for exactly that reason:
    `add_listener` is the RUNNING-device path and its whole added value is the
    subscription this one does not need yet.
    """
    devices = []
    for aggregator in provisioned.aggregators:
        device = MockAggregator(
            deployment_slug=provisioned.deployment_slug,
            aggregator_uuid=aggregator.aggregator_uuid,
            login=device_login(
                manifest, aggregator.aggregator_uuid, host=host, port=port, ca_cert=ca_cert
            ),
        )
        for mac in aggregator.macs:
            device.listeners[mac] = MockListener(mac=mac, aggregator=device)
        devices.append(device)
    return Fleet(devices, stagger=stagger)


def device_keys(revisions: Sequence[dict[str, Any]], fleet: ProvisionedFleet) -> set[str]:
    """Which of THIS fleet's devices a set of revisions was published to.

    Keyed the way the devices are: an `aggregator_uuid` for an Aggregator, a MAC
    for a Listener. The translation is needed because a revision addresses an
    Aggregator by its platform `id` — three identity columns, never conflated
    (spec 4.2) — while a device knows itself by the `aggregator_uuid` that
    appears in its topics.

    Revisions for devices outside this fleet are ignored rather than being an
    error: an apply can legitimately reach a deployment the runner does not hold
    every device of, and waiting for one of those would hang forever.
    """
    by_platform_id = {
        aggregator.aggregator_id: aggregator.aggregator_uuid for aggregator in fleet.aggregators
    }
    mine = {mac for aggregator in fleet.aggregators for mac in aggregator.macs}
    keys: set[str] = set()
    for revision in revisions:
        target = str(revision["target_id"])
        if revision["target_type"] == "aggregator":
            known = by_platform_id.get(target)
            if known is not None:
                keys.add(known)
        elif target in mine:
            keys.add(target)
    return keys


def assume_provisioned(plan: FleetPlan) -> ProvisionedFleet:
    """The fleet `--no-provision` claims already exists.

    Only what a DEVICE needs is reconstructed — the deployment slug, each
    `aggregator_uuid`, each MAC — all of which are pure functions of the plan,
    which is why `FleetPlan` computes rather than remembers. The platform-side
    ids are deliberately left empty: without a REST session there is nothing to
    publish a revision with, and `run()` refuses that combination rather than
    posting an apply for entity ids that are empty strings.
    """
    return ProvisionedFleet(
        deployment_id="",
        deployment_slug=plan.deployment_slug,
        aggregators=tuple(
            ProvisionedAggregator(
                index=index,
                aggregator_uuid=plan.aggregator_uuid(index),
                aggregator_id="",
                pod_id="",
                macs=plan.macs(index),
            )
            for index in plan.indices()
        ),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fleet.py",
        description="Run a mock fleet against a real broker and a real platform (task SIM.4)",
        epilog=(
            "Broker credentials come from the dev broker manifest and are minted from "
            "INVENTORY: provision first (python provision.py) and reload the broker "
            "(docker compose restart mosquitto) before running a fleet that has grown."
        ),
    )
    add_fleet_arguments(parser)
    parser.add_argument(
        "--broker",
        default=os.environ.get("EOE_SIM_BROKER", DEFAULT_BROKER),
        help=f"HOST:PORT the DEVICES dial (default {DEFAULT_BROKER})",
    )
    parser.add_argument(
        "--scenario",
        default=os.environ.get("EOE_SIM_SCENARIO", "") or None,
        help="scenario to inject once the fleet has converged (see --list-scenarios)",
    )
    parser.add_argument(
        "--scenario-devices",
        type=int,
        default=int(os.environ.get("EOE_SIM_SCENARIO_DEVICES", "0")),
        help="how many Aggregators the scenario applies to (default 0 = all of them)",
    )
    parser.add_argument(
        "--stagger",
        type=float,
        default=float(os.environ.get("EOE_SIM_STAGGER", str(DEFAULT_STAGGER))),
        help=f"seconds between device connects (default {DEFAULT_STAGGER}; 0 = a connect storm)",
    )
    parser.add_argument(
        "--wake-grace",
        type=int,
        default=int(os.environ.get("EOE_SIM_WAKE_GRACE", str(FLEET_SETTING_VALUE))),
        help=(
            f"value published for {FLEET_SETTING} (default {FLEET_SETTING_VALUE}). "
            "Re-running with the SAME value changes no device's effective config, so E2 "
            "cuts no revision and there is nothing to converge — pass a different one to "
            "make a repeat run do something."
        ),
    )
    parser.add_argument(
        "--apply-timeout",
        type=float,
        default=float(os.environ.get("EOE_SIM_APPLY_TIMEOUT", str(DEFAULT_APPLY_TIMEOUT))),
        help=f"seconds to wait for the fleet to converge (default {DEFAULT_APPLY_TIMEOUT:g})",
    )
    parser.add_argument(
        "--no-provision",
        action="store_true",
        help="assume inventory already exists; make no REST calls at all (implies --no-apply)",
    )
    parser.add_argument(
        "--no-apply",
        action="store_true",
        help="publish no revision; the fleet connects and applies whatever is already retained",
    )
    parser.add_argument(
        "--stay",
        action="store_true",
        help="keep the fleet up until SIGINT/SIGTERM instead of exiting once it has converged",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="print the shipped scenario catalogue and exit",
    )
    return parser.parse_args(argv)


def catalogue(scenarios: dict[str, Scenario]) -> str:
    """The shipped scenarios and the registry behind them, as text.

    Both halves, because they answer different questions: the files are what
    `--scenario` accepts, and the behaviours are what a new file may name.
    """
    lines = ["scenarios (--scenario NAME):"]
    for name, scenario in sorted(scenarios.items()):
        suffix = (
            ""
            if scenario.fleet_safe
            else f"  [suite only: needs {', '.join(scenario.unsafe_behaviours())}]"
        )
        lines.append(f"  {name:24} {scenario.description}{suffix}")
        lines.append(f"  {'':24} expects: {scenario.expects}")
    lines.append("")
    lines.append("behaviours a scenario file may name:")
    lines.extend(f"  {name:24} {model.summary()}" for name, model in sorted(BEHAVIOURS.items()))
    return "\n".join(lines)


def choose_scenario(scenarios: dict[str, Scenario], name: str | None) -> Scenario | None:
    """Resolve `--scenario` at STARTUP, or explain why it cannot be run.

    Both refusals happen before a single socket is opened, which is the whole
    value of them: a name typo and a structurally impossible scenario are both
    things to learn in the first second (D107, D113).
    """
    if name is None:
        return None
    scenario = scenarios.get(name)
    if scenario is None:
        raise ScenarioError(
            f"{name!r} is not a shipped scenario; known scenarios are "
            f"{', '.join(sorted(scenarios))} (see --list-scenarios)"
        )
    if not scenario.fleet_safe:
        raise ScenarioError(
            f"the {name!r} scenario cannot run against a provisioned fleet: "
            f"{', '.join(scenario.unsafe_behaviours())} needs a device this runner cannot "
            "create — one inventory has never heard of, or one claiming another parent's MAC. "
            "It is exercised in sim/tests/test_scenario_outcomes.py, which stages exactly that."
        )
    return scenario


async def run(args: argparse.Namespace) -> int:
    """Bring a fleet up, converge it, misbehave, and take it down.

    The body sits inside one `try`/`finally` whose `finally` is the polite
    shutdown, because every failure mode here — a refused connect, a revision
    that never arrived, a Ctrl-C — otherwise leaves live MQTT sessions holding
    retained `online` messages, and leaving those behind is how a harness
    teaches a platform to believe in devices that are gone.
    """
    plan = plan_from(args)
    host, port = broker_endpoint(args.broker, default_port=8883)
    ca_cert = args.certs / "ca.crt"
    if not ca_cert.is_file():
        raise ProvisionError(
            f"{ca_cert} does not exist: the dev broker's CA is what a device verifies the "
            "broker against. Run `uv run python -m app.devbroker --certs-only` from backend/."
        )
    if args.no_provision and not args.no_apply:
        raise ProvisionError(
            "--no-provision makes no REST calls, so this run has no inventory ids to publish "
            "a revision against. Pass --no-apply as well (the fleet then applies whatever is "
            "retained), or drop --no-provision and let it provision — it is idempotent."
        )

    # Loaded before anything connects, and before the API is even dialled: an
    # unloadable catalogue is a repository problem, not a fleet problem (D107).
    scenario = choose_scenario(load_scenarios(), args.scenario)

    operator: Operator | None = None if args.no_provision else operator_from(args)
    provisioned = (
        assume_provisioned(plan) if operator is None else provision_hierarchy(operator, plan)
    )
    fleet = build_fleet(
        provisioned,
        load_accounts(args.certs),
        host=host,
        port=port,
        ca_cert=ca_cert,
        stagger=args.stagger,
    )

    log.info("bringing up %s against %s:%d", plan, host, port)
    try:
        await fleet.connect()
        if operator is not None and not args.no_apply:
            revisions = apply_fleet_config(operator, provisioned.aggregator_ids, args.wake_grace)
            told = device_keys(revisions, provisioned)
            if not told:
                print(
                    f"nothing to publish: the fleet already holds "
                    f"{FLEET_SETTING}={args.wake_grace}, so no device's effective config "
                    "changed and E2 correctly cut no revision. Pass a different "
                    "--wake-grace to make this run change something."
                )
            elapsed = await fleet.wait_for_applies(args.apply_timeout, only=told)
            print(f"time-to-all-applied: {elapsed:.1f}s ({len(told)} device(s) told)")
        if scenario is not None:
            await fleet.run_scenario(scenario, devices=args.scenario_devices)
        if args.stay:
            log.info("fleet is up; waiting for SIGINT/SIGTERM")
            await _stop_event().wait()
    finally:
        # Counted BEFORE the shutdown, so the report describes the fleet that
        # ran rather than the empty one that is left.
        counters = fleet.counters()
        await fleet.shutdown()
        print(counters.render())
        peak = peak_rss_mib()
        print(f"peak resident set size: {'unavailable' if peak is None else f'{peak:.0f} MiB'}")
    return 0


def _stop_event() -> asyncio.Event:
    """An event SIGINT and SIGTERM set, so `--stay` ends in a polite shutdown.

    Installed HERE rather than at start-up, and that is deliberate: while the
    fleet is connecting, the default handlers are what an operator wants — a
    Ctrl-C during a six-hundred-Listener bring-up should raise KeyboardInterrupt
    and land in the `finally` above, not be swallowed until the wait that has
    not started yet.

    `docker compose down` sends SIGTERM, and the default handler for that is a
    process that vanishes: every device's will fires, and the run is
    indistinguishable from a crash. Platforms with no `add_signal_handler`
    (Windows) fall through to KeyboardInterrupt, which `main` already handles.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signalnum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, AttributeError):
            loop.add_signal_handler(signalnum, stop.set)
    return stop


def peak_rss_mib() -> float | None:
    """Peak resident set size of this process in MiB, or None where unknowable.

    Reported because the phase document asks a full-scale run to record memory,
    and a number the runner printed itself is one nobody has to reproduce with a
    stopwatch beside `docker stats`. `resource` is POSIX-only — the gate also
    runs on Windows — and `ru_maxrss` is KiB on Linux, which is where the
    documented load run happens.
    """
    try:
        import resource
    except ImportError:  # Windows
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_arguments(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    if args.list_scenarios:
        try:
            print(catalogue(load_scenarios()))
        except ScenarioError as error:
            print(f"the scenario catalogue is unloadable: {error}", file=sys.stderr)
            return 1
        return 0
    try:
        return asyncio.run(run(args))
    except (ProvisionError, ScenarioError) as error:
        print(f"fleet did not start: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (RuntimeError, TimeoutError) as error:
        print(f"fleet run failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
