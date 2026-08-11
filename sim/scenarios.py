"""Injectable misbehaviours, and the TOML files that name them (task SIM.3).

A scenario is a **declarative** way to make the fleet do something the platform
is supposed to notice. The registry below is the extension point; the files in
`sim/scenarios/` are the interface later epics use without writing code (phase
doc fixed choice, matching D5's config-file choice).

Two rules run through everything here:

**Every misbehaviour is produced the way a real device produces it.** A drifted
revision comes from a device reporting a config that differs, carrying the
checksum of what it actually holds; an offline Aggregator comes from a socket
dying without a DISCONNECT; a quarantined identity comes from an Aggregator
publishing on a MAC that inventory has filed under someone else. Nothing here
writes to a database, and nothing here changes platform behaviour to make a
scenario work. If a failure cannot be produced by publishing the bytes a real
device publishes, it is not one a real device can produce either, and the
finding is worth more than the test.

**A bad scenario file fails at LOAD.** An unknown behaviour name or an
out-of-range parameter is a typo, and a typo has to be reported with the file
and the key that carry it, in the second before a run starts — not at hour two
of a load run, with a fleet up and nothing to show for it. That is what the
Pydantic model on every behaviour is for: `extra="forbid"` turns a misspelled
parameter into an error naming the key, and field constraints turn an
impossible value into an error naming the bound it broke.

The file format, one behaviour per `[[behaviour]]` table:

    name = "drift"
    description = "A device quietly holding something other than what it was sent."
    expects = "the revision reaches `drifted`"

    [[behaviour]]
    name = "drift"
    key = "logging.verbosity"
    value = "trace"

`name` matches the file stem, because a scenario whose name disagrees with its
filename is a rename someone stopped halfway through and every later reference
picks one of the two.
"""

import asyncio
import logging
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from device import MockAggregator

# The published contract reaches this module the way it reaches `device.py`:
# by path (see that module's note). Imported here for its FIELD TYPES — a MAC
# and an event code that a scenario file supplies are validated against the
# same rules the wire enforces, so a malformed one is refused at load rather
# than by a broker at hour two.
_BACKEND = str(Path(__file__).resolve().parent.parent / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.contracts.mqtt import EventCode, MacAddress  # noqa: E402  (path first)

log = logging.getLogger(__name__)

#: Where the shipped catalogue lives. A directory of data beside a module of
#: the same name: the module is the registry, the directory is the interface.
SCENARIO_DIR: Final = Path(__file__).resolve().parent / "scenarios"

#: Bound on any "wait for the platform to do its half" parameter. Long enough
#: for a real publish-apply round trip on a loaded host, short enough that a
#: scenario file with a typo'd unit (seconds meant, minutes typed) fails at
#: load instead of hanging a fleet run for a week.
MAX_WAIT_SECONDS: Final = 3600.0


class ScenarioError(ValueError):
    """A scenario file that cannot be trusted to describe a run.

    Every message names the file, and where a key is at fault, the key. This
    is the only exception `load_scenario` raises: callers of a loader want one
    thing to catch, and a TOML syntax error and an out-of-range parameter are
    the same problem to whoever has to fix them.
    """


@dataclass(frozen=True)
class ScenarioContext:
    """What a behaviour is pointed at: one connected device.

    A dataclass rather than a bare `MockAggregator` argument so SIM.4 can hand
    behaviours more of the fleet (counters, a clock, sibling devices) without
    changing the signature of every behaviour that never needed it.
    """

    aggregator: MockAggregator


class Behaviour(BaseModel):
    """One injectable misbehaviour: its parameters AND what it does.

    The parameters are the model's fields, so a scenario file's table IS the
    constructor call and validation is Pydantic's rather than a hand-written
    check that drifts from the docstring. `extra="forbid"` is load-bearing: a
    misspelled key in a TOML file must be an error, because the alternative is
    a scenario that silently runs with the default and a run that proves
    nothing.
    """

    model_config = ConfigDict(extra="forbid")

    async def run(self, context: ScenarioContext) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    @classmethod
    def summary(cls) -> str:
        """The first line of the behaviour's docstring, for catalogue listings
        (SIM.4's CLI). Written here so a behaviour has exactly one description
        and it is the one a maintainer reads next to the code."""
        return (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""


#: name -> behaviour, in registration order. The catalogue the project plan
#: names is exactly the six below, and `tests/test_scenarios.py` asserts every
#: registered behaviour ships a scenario file so that a behaviour added
#: without one cannot go unexercised.
BEHAVIOURS: Final[dict[str, type[Behaviour]]] = {}


def behaviour[B: Behaviour](name: str) -> Callable[[type[B]], type[B]]:
    """Register one behaviour under the name scenario files use.

    The name is written here rather than derived from the class name so the
    wire-visible vocabulary is not a rename away from changing: a class can be
    renamed for clarity, every shipped TOML file cannot.
    """

    def register(cls: type[B]) -> type[B]:
        if name in BEHAVIOURS:
            raise ScenarioError(f"behaviour {name!r} is registered twice")
        BEHAVIOURS[name] = cls
        return cls

    return register


# --- the catalogue ----------------------------------------------------------


@behaviour("apply_error")
class ApplyError(Behaviour):
    """A device that cannot honour a setting and applies something else.

    Installs an apply hook, so the NEXT revision this device receives is
    applied with `key` forced to `value` — and reported, with the checksum of
    what it genuinely now holds. The platform sees a coherent report of a
    config that is not the revision's while that revision is `pending`, which
    is spec 6.2's "device reports an error applying": a definite negative
    answer, so the revision fails immediately rather than waiting out the
    pending window and blaming a timeout for a device that answered at once.

    Deliberately not an event: a device that only complained on the event
    topic while reporting the right config would leave the platform correct to
    mark the revision applied, and this scenario is about the reported state.
    """

    key: str = Field(default="logging.verbosity", min_length=1)
    value: JsonValue = "sim-could-not-apply"

    async def run(self, context: ScenarioContext) -> None:
        key, value = self.key, self.value

        def could_not_apply(config: dict[str, Any]) -> dict[str, Any]:
            return {**config, key: value}

        context.aggregator.apply_hook = could_not_apply
        log.info("%s will refuse %s on its next revision", context.aggregator, key)


@behaviour("drift")
class Drift(Behaviour):
    """A device that applied a revision and later holds something else.

    Drift as the field produces it: somebody changed a setting locally, or
    firmware retuned itself. The device keeps naming the revision it applied
    and reports the config it ACTUALLY has, with that config's checksum — the
    only way the platform can tell, and the reason the checksum is computed by
    the device rather than echoed from the desired payload.

    Waits for an apply first, because `applied -> drifted` is the only edge
    spec 6.2 has here: a device that drifts before it ever converged has
    nothing to diverge from and the platform would rightly record nothing.
    """

    key: str = Field(default="logging.verbosity", min_length=1)
    value: JsonValue = "trace"
    wait_seconds: float = Field(default=60.0, gt=0, le=MAX_WAIT_SECONDS)

    async def run(self, context: ScenarioContext) -> None:
        aggregator = context.aggregator
        await aggregator.wait_for_apply(timeout=self.wait_seconds)
        aggregator.config = {**aggregator.config, self.key: self.value}
        await aggregator.report()
        log.info("%s drifted on %s", aggregator, self.key)


@behaviour("disconnect")
class Disconnect(Behaviour):
    """A device that dies without saying goodbye.

    The socket goes and no DISCONNECT packet is sent, so the broker publishes
    the will the device registered before it ever connected, and the platform
    learns the device is offline from the only signal spec 9.3 makes
    authoritative. A polite `disconnect()` here would prove the opposite thing.
    """

    after_seconds: float = Field(default=0.0, ge=0, le=MAX_WAIT_SECONDS)

    async def run(self, context: ScenarioContext) -> None:
        if self.after_seconds:
            await asyncio.sleep(self.after_seconds)
        await context.aggregator.kill()


@behaviour("missed_wake_window")
class MissedWakeWindow(Behaviour):
    """A Listener that declares an off-window and never comes back.

    The Listener promises to wake at `now + sleep_seconds` over the local link
    and then does nothing. Its Aggregator — which holds the promise and the
    grace period from its own `listener.wake_grace_seconds` — raises
    `listener_missed_wake_window` once the two have elapsed and reports the
    Listener offline. Nothing here counts the grace: this behaviour only makes
    the promise, so the sweep that catches it is the one a real Aggregator runs.

    The wait is therefore `sleep_seconds` plus whatever grace the device has
    been CONFIGURED with, which is a property worth remembering when a run
    seems slow: publish a shorter `listener.wake_grace_seconds` and the same
    scenario resolves faster, because the setting genuinely drives the device.
    """

    mac: MacAddress | None = None
    sleep_seconds: float = Field(default=1.0, gt=0, le=MAX_WAIT_SECONDS)

    async def run(self, context: ScenarioContext) -> None:
        aggregator = context.aggregator
        listener = aggregator.listener(self.mac) if self.mac else aggregator.first_listener()
        await listener.declare_sleep(seconds=self.sleep_seconds)


@behaviour("duplicate_mac")
class DuplicateMac(Behaviour):
    """An Aggregator reporting a Listener MAC that belongs to another parent.

    Two devices flashed with one identity, or a Listener physically moved
    between Pods without inventory being told. The reporting Aggregator
    attaches the MAC and reports on it from its own subtree — which its ACL
    allows, because a broker cannot know whose MAC is whose — and the platform
    resolves the identity through the E1.5 services: the report is
    quarantined, a `duplicate_identity` alert opens, and **the inventory row is
    not touched** (spec 4.3 item 2, "rather than overwriting inventory").

    The MAC is required and typed as the contract's `MacAddress`, so a
    scenario file carrying a malformed one fails at load rather than at the
    first publish.
    """

    mac: MacAddress

    async def run(self, context: ScenarioContext) -> None:
        listener = await context.aggregator.add_listener(self.mac)
        await listener.report()
        log.info("%s claimed %s", context.aggregator, self.mac)


@behaviour("unprovisioned_aggregator")
class UnprovisionedAggregator(Behaviour):
    """A device the platform has never heard of, talking anyway.

    Hardware in the field that inventory does not list — decommissioned,
    re-flashed, or shipped before anyone entered it. The behaviour itself is
    ordinary: the device simply reports, and optionally raises an event. The
    misbehaviour is WHO is talking, so this scenario belongs on a device whose
    `aggregator_uuid` matches no inventory row; the platform answers with the
    spec 4.3 item 3 membership check and opens a `provisioning_required` alert
    on every channel it reaches.
    """

    event_code: EventCode | None = None

    async def run(self, context: ScenarioContext) -> None:
        await context.aggregator.report()
        if self.event_code is not None:
            await context.aggregator.publish_event(
                self.event_code, level="warn", detail="reporting without a provisioning record"
            )


# --- scenario files ---------------------------------------------------------


class _ScenarioFile(BaseModel):
    """The shape of a scenario TOML file, so its own typos are load errors too.

    `expects` is prose and is not asserted against anything at runtime — it is
    what the file says the PLATFORM should end up doing, kept beside the
    behaviours because a scenario nobody can read the expected outcome of gets
    run once and interpreted differently every time after.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    expects: str = Field(min_length=1)
    behaviour: list[dict[str, Any]] = Field(min_length=1)


@dataclass(frozen=True)
class Scenario:
    """One loaded, fully validated scenario. Nothing here can still fail on a
    typo: every behaviour is an instance, so construction already happened."""

    name: str
    description: str
    expects: str
    behaviours: tuple[Behaviour, ...]
    source: Path

    async def run(self, context: ScenarioContext) -> None:
        """Run the behaviours in file order, which is the order they are
        written in and the only order a reader can predict."""
        log.info("running scenario %s against %s", self.name, context.aggregator)
        for item in self.behaviours:
            await item.run(context)


def _problems(error: ValidationError) -> str:
    """Pydantic's complaints as one line, each naming its key.

    `include_input=False` for the contract module's reason: a value out of a
    scenario file is fine to echo, but this helper is one edit away from being
    pointed at a payload, and the habit is cheaper than the incident.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'file'}: {item['msg']}"
        for item in error.errors(include_url=False, include_input=False, include_context=False)
    )


def load_scenario(path: Path) -> Scenario:
    """Load and fully validate one scenario file, or raise `ScenarioError`.

    Every failure below names the file, and every failure about a value names
    the key. That is the acceptance criterion for this task and it is worth
    being pedantic about: the alternative is a fleet run that starts, does
    something almost right for two hours, and leaves an operator reading MQTT
    logs to work out which line of which file was wrong.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ScenarioError(f"{path.name}: not valid TOML: {error}") from error
    except OSError as error:
        raise ScenarioError(f"{path}: cannot be read: {error}") from error

    try:
        parsed = _ScenarioFile.model_validate(raw)
    except ValidationError as error:
        raise ScenarioError(f"{path.name}: {_problems(error)}") from error

    if parsed.name != path.stem:
        raise ScenarioError(
            f"{path.name}: 'name' is {parsed.name!r} but the file is {path.stem!r}; "
            "a scenario is referred to by one of the two and they must agree"
        )

    behaviours: list[Behaviour] = []
    for index, table in enumerate(parsed.behaviour):
        where = f"{path.name}: behaviour[{index}]"
        parameters = dict(table)
        name = parameters.pop("name", None)
        if not isinstance(name, str) or not name:
            raise ScenarioError(f"{where}: 'name' is required and must name a behaviour")
        model = BEHAVIOURS.get(name)
        if model is None:
            raise ScenarioError(
                f"{where}: 'name': {name!r} is not a known behaviour; "
                f"known behaviours are {', '.join(sorted(BEHAVIOURS))}"
            )
        try:
            behaviours.append(model.model_validate(parameters))
        except ValidationError as error:
            raise ScenarioError(f"{where} ({name}): {_problems(error)}") from error

    return Scenario(
        name=parsed.name,
        description=parsed.description,
        expects=parsed.expects,
        behaviours=tuple(behaviours),
        source=path,
    )


def load_scenarios(directory: Path = SCENARIO_DIR) -> dict[str, Scenario]:
    """Every scenario in a directory, keyed by name, sorted by filename.

    All of them, eagerly, and the first bad one stops the load. A loader that
    skipped the file it could not parse would let a fleet run start with the
    scenario the operator asked for silently missing.
    """
    if not directory.is_dir():
        raise ScenarioError(f"{directory}: no such scenario directory")
    scenarios: dict[str, Scenario] = {}
    for path in sorted(directory.glob("*.toml")):
        scenario = load_scenario(path)
        scenarios[scenario.name] = scenario
    if not scenarios:
        raise ScenarioError(f"{directory}: holds no scenario files")
    return scenarios
