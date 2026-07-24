"""R0 gate enforcement runner (.claude/rules/project-rules.json).

pytest 9 ignores mutation of ``session.exitstatus`` inside
``pytest_sessionfinish`` (DECISIONS D11), so a conftest hook cannot turn a
skipped, xfailed, or deselected test into a nonzero exit. The gate scripts
invoke this wrapper instead: it runs the entire suite with no filter arguments,
reads the counts after the run, and fails the process itself. It fails closed
when the counts cannot be read at all.
"""

import os
import sys

import pytest

GUARDED_KEYS = ("skipped", "xfailed", "deselected")


class GateGuard:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def pytest_sessionfinish(self, session) -> None:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is None:
            return
        self.counts = {key: len(reporter.stats.get(key, [])) for key in GUARDED_KEYS}


def enforce(counts: dict[str, int], code: int) -> int:
    """Turn the guarded counts into the gate's exit code (rule R0).

    Empty counts mean the guard never saw a report: fail closed. Any nonzero
    guarded count is a violation regardless of the suite's own exit code.
    """
    if not counts:
        print("gate guard: could not read test counts; failing closed (rule R0)", file=sys.stderr)
        return 2
    if any(counts.values()):
        print(f"gate guard: R0 violation {counts}; failing the gate", file=sys.stderr)
        return 1
    return code


def main() -> int:
    os.environ["EOE_GATE"] = "1"
    guard = GateGuard()
    # No arguments are forwarded: narrowing the gate suite is forbidden (R0).
    code = int(pytest.main([], plugins=[guard]))
    return enforce(guard.counts, code)


if __name__ == "__main__":
    sys.exit(main())
