"""The rules that make this harness worth running (task SIM.1).

Two of the phase's fixed choices are properties of the whole `/sim` tree
rather than of any one function, so they are asserted over the tree:

* **No topic string and no control-plane body is built by hand.** A
  hand-rolled `f"eoe/..."` is a defect even when it happens to be correct,
  because the point of the harness is to prove the PUBLISHED contract is
  sufficient for someone who was given nothing else.
* **The harness imports the wire contract and nothing else from the
  platform.** It is a client, with no privileged access and no reach into a
  database to make a scenario work. A behaviour that cannot be produced by
  publishing the bytes a real device publishes is not a behaviour a real
  device can produce either.

The suite is exempt from the second rule and only from the second: an
acceptance test that says "against a real platform" has to drive one.
"""

import tomllib
from pathlib import Path

from app.contracts.mqtt import ROOT

SIM = Path(__file__).resolve().parents[1]
REPO_ROOT = SIM.parent
#: The one module in `/sim` a device is allowed to import from the platform.
PUBLISHED_CONTRACT = "app.contracts.mqtt"


def harness_modules() -> list[Path]:
    """Every harness module: `/sim`'s own code, tests excluded."""
    return sorted(path for path in SIM.glob("*.py"))


def sim_sources() -> list[Path]:
    """Every Python file under `/sim`, this one excluded — it necessarily
    contains the strings it is looking for."""
    here = Path(__file__).resolve()
    return sorted(path for path in SIM.rglob("*.py") if path != here)


def test_no_topic_string_is_built_by_hand_anywhere_in_sim():
    """The topic root, taken from the contract rather than spelled out here,
    so this test cannot pass by agreeing with itself about the namespace."""
    forbidden = f"{ROOT}/"
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in sim_sources()
        if forbidden in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{offenders} spell a topic namespace out by hand; use the contract's topic builders"
    )


def test_the_harness_imports_only_the_published_contract_from_the_platform():
    for path in harness_modules():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import app", "from app")):
                continue
            assert stripped.startswith(f"from {PUBLISHED_CONTRACT} import"), (
                f"{path.name} reaches into the platform beyond the published contract: {stripped}"
            )


def test_sims_dev_group_carries_the_whole_platform_runtime_set():
    """SIM.1's acceptance runs a real platform in the test process, so every
    backend runtime dependency has to be installable here — and has to stay
    installable. Without this, adding a dependency to the backend turns the
    sim suite red weeks later with an ImportError that names a package nobody
    remembers deciding to need.
    """
    backend = tomllib.loads((REPO_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    sim = tomllib.loads((SIM / "pyproject.toml").read_text(encoding="utf-8"))
    available = set(sim["project"]["dependencies"]) | set(sim["dependency-groups"]["dev"])
    missing = sorted(set(backend["project"]["dependencies"]) - available)
    assert missing == [], (
        f"backend/pyproject.toml requires {missing}, which sim cannot install; "
        "add them to sim's dev group with the same specifiers"
    )


def test_the_lint_and_type_settings_match_the_backends():
    """E0 set the style and this phase does not relitigate it (rule R2). A
    `/sim` on different rules would fail CI for formatting nobody chose."""
    backend = tomllib.loads((REPO_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    sim = tomllib.loads((SIM / "pyproject.toml").read_text(encoding="utf-8"))
    assert sim["tool"]["ruff"] == backend["tool"]["ruff"]
    assert sim["tool"]["ruff"]["lint"]["select"] == backend["tool"]["ruff"]["lint"]["select"]
    assert sim["tool"]["mypy"]["strict"] is True
    assert sim["tool"]["mypy"]["python_version"] == backend["tool"]["mypy"]["python_version"]
