"""R0 gate enforcement for `/sim` (task SIM.5).

`/sim` is its own uv project with its own suite, so `sh gate.sh sim-protocol`
cannot run the backend's runner — but the RULE it enforces is the same one, and
there is no version of R0 that is different here. So this file is a launcher and
not a second implementation: `GateGuard` and `enforce` are imported from
`backend/tests/gate_runner.py`, the module that owns them.

Why a wrapper exists at all (DECISIONS D11): pytest 9 ignores mutation of
`session.exitstatus` inside `pytest_sessionfinish`, so a conftest hook cannot
turn a skipped, xfailed, or deselected test into a nonzero exit. The gate scripts
invoke this instead — it runs the entire suite with no filter arguments, reads the
counts afterwards, and fails the process itself. It fails closed when the counts
cannot be read at all.
"""

import sys
from pathlib import Path

import pytest

#: `backend/tests`, where the one implementation of this lives. `/sim` reaches
#: the platform by path everywhere else too (phase doc SIM.1 fixed choice); this
#: is the same move for a test helper, and `tests/conftest.py` puts the same
#: directory on the path for the suite itself.
BACKEND_TESTS = Path(__file__).resolve().parents[2] / "backend" / "tests"
if str(BACKEND_TESTS) not in sys.path:
    sys.path.insert(0, str(BACKEND_TESTS))

from gate_runner import GateGuard, enforce  # noqa: E402  (the path above has to be set first)


def main() -> int:
    guard = GateGuard()
    # No arguments are forwarded: narrowing the gate suite is forbidden (R0).
    code = int(pytest.main([], plugins=[guard]))
    return enforce(guard.counts, code)


if __name__ == "__main__":
    sys.exit(main())
