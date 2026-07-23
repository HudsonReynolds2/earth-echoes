"""Shared test configuration.

R0 gate awareness (.claude/rules/project-rules.json): when EOE_GATE=1, any
skipped, xfailed, or deselected test is a gate violation. Enforcement (the
nonzero exit) lives in tests/gate_runner.py, which the gate scripts invoke,
because pytest 9 ignores exitstatus mutation in this hook (DECISIONS D11).
This hook prints the violation loudly so plain `pytest` runs surface it too.
Filtered runs stay legal while debugging between gates; they are only
forbidden as a way to clear a gate.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_git(*args: str) -> str:
    """Run git in the repo root, decoding output as UTF-8 explicitly.

    Windows would otherwise decode subprocess output with the ANSI code page
    (cp1252), which breaks on the spec's UTF-8 diagrams (see DECISIONS D10).
    """
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("EOE_GATE") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    counts = {key: len(reporter.stats.get(key, [])) for key in ("skipped", "xfailed", "deselected")}
    if any(counts.values()):
        reporter.write_line(
            f"EOE_GATE violation (rule R0): {counts}. "
            "Skipped, xfailed, and deselected tests are hard failures at a gate; "
            "tests/gate_runner.py turns this into a failing exit.",
            red=True,
        )
