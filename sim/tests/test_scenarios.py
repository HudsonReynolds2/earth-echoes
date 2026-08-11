"""SIM.3: what a scenario file is, and what a bad one does (no containers).

Half of this task's acceptance is about failure timing: an unknown behaviour
name or an out-of-range parameter must fail **at load**, with a message naming
the file and the key. The other half — that every shipped scenario actually
produces the platform reaction it claims — needs a platform, and lives in
`test_scenario_outcomes.py`.

Why the timing matters enough to be its own file: a scenario is fed to a fleet
runner that brings up twenty Aggregators, waits for them to converge, and then
misbehaves on purpose. A typo discovered at that point costs the whole run and
looks like a platform bug. Discovered at load it costs a second and names the
line.
"""

import pytest
from app.contracts.mqtt import EVENT_LISTENER_MISSED_WAKE_WINDOW

from scenarios import (
    BEHAVIOURS,
    SCENARIO_DIR,
    Behaviour,
    Scenario,
    ScenarioError,
    load_scenario,
    load_scenarios,
)

#: The catalogue the project plan names, exactly. Written out rather than
#: derived from the registry, so that deleting a behaviour and its file
#: together still fails here: this list is the plan's, and the plan is not
#: satisfied by a consistent subset of itself.
CATALOGUE = {
    "apply_error",
    "drift",
    "disconnect",
    "missed_wake_window",
    "duplicate_mac",
    "unprovisioned_aggregator",
}


def write(tmp_path, name: str, body: str):
    path = tmp_path / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --- the shipped catalogue --------------------------------------------------


def test_every_shipped_scenario_loads():
    """ACCEPTANCE (phase doc SIM.3), the first half of it: every scenario file
    in the repository loads. Nothing is skipped and nothing is tolerated — a
    loader that ignored the file it could not parse would let a run start with
    the scenario the operator asked for silently missing."""
    scenarios = load_scenarios()

    assert set(scenarios) == CATALOGUE
    for scenario in scenarios.values():
        assert isinstance(scenario, Scenario)
        assert scenario.behaviours, f"{scenario.name} names no behaviour to run"
        assert scenario.source.parent == SCENARIO_DIR


def test_every_scenario_says_what_the_platform_should_do():
    """`expects` is prose and nothing asserts against it at runtime, which is
    exactly why it has to be there: a scenario whose expected outcome is not
    written down gets run once and interpreted differently every time after."""
    for scenario in load_scenarios().values():
        assert scenario.expects.strip(), f"{scenario.name} does not say what it expects"
        assert scenario.description.strip()


def test_every_registered_behaviour_ships_a_scenario_file():
    """The registry is the extension point and the files are the interface. A
    behaviour with no file is one nobody can run without writing code, and one
    this suite would never exercise — which is how an injectable failure
    quietly stops working."""
    named = {
        item.__class__ for scenario in load_scenarios().values() for item in scenario.behaviours
    }
    missing = sorted(name for name, model in BEHAVIOURS.items() if model not in named)

    assert missing == [], f"{missing} are registered but no scenario file names them"


def test_the_registry_is_the_catalogue_the_plan_names():
    assert set(BEHAVIOURS) == CATALOGUE
    for name, model in BEHAVIOURS.items():
        assert issubclass(model, Behaviour)
        assert model.summary(), f"{name} has no docstring to describe it"


# --- failing at load --------------------------------------------------------


def test_an_unknown_behaviour_name_fails_at_load_naming_the_file(tmp_path):
    """ACCEPTANCE (phase doc SIM.3): not at hour two of a load run.

    The message carries the file, the key, the value that was wrong AND the
    behaviours that would have worked — because the fix for a typo is a
    correction, and a reader who has to go and find the list is a reader who
    guesses again.
    """
    path = write(
        tmp_path,
        "typo",
        'name = "typo"\ndescription = "d"\nexpects = "e"\n[[behaviour]]\nname = "drfit"\n',
    )

    with pytest.raises(ScenarioError) as error:
        load_scenario(path)

    assert "typo.toml" in str(error.value)
    assert "'name'" in str(error.value)
    assert "'drfit'" in str(error.value)
    assert "drift" in str(error.value), "the message does not offer the behaviours that exist"


def test_an_out_of_range_parameter_fails_at_load_naming_the_key(tmp_path):
    """ACCEPTANCE (phase doc SIM.3). `sleep_seconds` is bounded because a
    scenario whose unit was typed wrong (minutes for seconds, milliseconds for
    seconds) would otherwise hang a fleet run for a week rather than fail."""
    path = write(
        tmp_path,
        "slow",
        'name = "slow"\ndescription = "d"\nexpects = "e"\n'
        '[[behaviour]]\nname = "missed_wake_window"\nsleep_seconds = 99999999.0\n',
    )

    with pytest.raises(ScenarioError) as error:
        load_scenario(path)

    assert "slow.toml" in str(error.value)
    assert "sleep_seconds" in str(error.value)


def test_a_parameter_that_cannot_be_zero_is_refused(tmp_path):
    """A wake declaration of zero seconds is a Listener that is already late,
    which is not an off-window at all."""
    path = write(
        tmp_path,
        "instant",
        'name = "instant"\ndescription = "d"\nexpects = "e"\n'
        '[[behaviour]]\nname = "missed_wake_window"\nsleep_seconds = 0.0\n',
    )

    with pytest.raises(ScenarioError, match="sleep_seconds"):
        load_scenario(path)


def test_a_misspelled_parameter_is_an_error_and_not_a_default(tmp_path):
    """`extra="forbid"` doing its job. A misspelled key silently falling back
    to the default is the worst outcome available: the run completes, proves
    something other than what was asked, and says nothing."""
    path = write(
        tmp_path,
        "sloppy",
        'name = "sloppy"\ndescription = "d"\nexpects = "e"\n'
        '[[behaviour]]\nname = "drift"\nkeey = "logging.verbosity"\n',
    )

    with pytest.raises(ScenarioError) as error:
        load_scenario(path)

    assert "sloppy.toml" in str(error.value)
    assert "keey" in str(error.value)


def test_a_malformed_mac_is_refused_before_anything_connects(tmp_path):
    """The MAC is typed as the CONTRACT's `MacAddress`, so a scenario file
    carrying an un-normalized or malformed one fails here rather than at the
    first publish — where it would surface as a topic error from inside a
    device, hours later."""
    path = write(
        tmp_path,
        "lower",
        'name = "lower"\ndescription = "d"\nexpects = "e"\n'
        '[[behaviour]]\nname = "duplicate_mac"\nmac = "02:ee:0e:01:01:01"\n',
    )

    with pytest.raises(ScenarioError, match="mac"):
        load_scenario(path)


def test_a_missing_required_parameter_names_itself(tmp_path):
    path = write(
        tmp_path,
        "nameless",
        'name = "nameless"\ndescription = "d"\nexpects = "e"\n'
        '[[behaviour]]\nname = "duplicate_mac"\n',
    )

    with pytest.raises(ScenarioError, match="mac"):
        load_scenario(path)


def test_a_scenario_whose_name_disagrees_with_its_file_is_refused(tmp_path):
    """A scenario is referred to by one of the two, and later epics will pick
    whichever is nearer to hand. They have to agree."""
    path = write(
        tmp_path,
        "one-name",
        'name = "another"\ndescription = "d"\nexpects = "e"\n[[behaviour]]\nname = "disconnect"\n',
    )

    with pytest.raises(ScenarioError, match="one-name"):
        load_scenario(path)


def test_a_file_with_no_behaviours_is_refused(tmp_path):
    path = write(tmp_path, "empty", 'name = "empty"\ndescription = "d"\nexpects = "e"\n')

    with pytest.raises(ScenarioError, match="empty.toml"):
        load_scenario(path)


def test_a_missing_top_level_key_names_itself(tmp_path):
    path = write(tmp_path, "bare", 'name = "bare"\n[[behaviour]]\nname = "disconnect"\n')

    with pytest.raises(ScenarioError) as error:
        load_scenario(path)

    assert "expects" in str(error.value)
    assert "description" in str(error.value)


def test_broken_toml_names_the_file(tmp_path):
    path = write(tmp_path, "broken", "name = \nthis is not toml")

    with pytest.raises(ScenarioError, match="broken.toml"):
        load_scenario(path)


def test_a_directory_with_no_scenarios_is_refused(tmp_path):
    """Loading nothing successfully is how a fleet run ends up doing nothing
    and reporting success."""
    with pytest.raises(ScenarioError, match="no scenario files"):
        load_scenarios(tmp_path)


# --- what the files say -----------------------------------------------------


def test_the_missed_wake_scenario_leaves_the_grace_period_to_the_device():
    """The behaviour makes the promise and stops. Nothing in a scenario file
    counts the grace period, because spec 6.5 puts that decision in the
    Aggregator — a scenario that timed it itself would be modelling a device
    that does not exist, and the event code it produced would be a lie."""
    scenario = load_scenarios()["missed_wake_window"]

    behaviour = scenario.behaviours[0]
    assert behaviour.sleep_seconds > 0
    assert not hasattr(behaviour, "grace_seconds")
    assert EVENT_LISTENER_MISSED_WAKE_WINDOW in scenario.expects.replace("`", "")
