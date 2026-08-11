"""Sim's test configuration (task SIM.1).

Two jobs, and the first is not to grow a second copy of anything.

**The fixtures are the backend's.** `backend/tests/conftest.py` owns the only
Mosquitto fixture in this repository and it stays that way (phase doc section
2): `ephemeral_broker`, `ephemeral_postgres`, `free_port`, `make_kek` and
`docker_retry` are loaded from it and re-exported here, so a sim test writes
`from conftest import ephemeral_broker` exactly as a backend test does. A
second broker fixture would drift from the first the day one of them learned
something about Docker Desktop that the other did not.

**The D99 parallel conventions are copied, because they have to be.** Every
live test here starts its own Postgres and Mosquitto; splitting one module
across xdist workers would start those containers once per worker and make the
suite slower rather than faster. See the hook below for why `tryfirst` is
load-bearing.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
BACKEND_TESTS = BACKEND / "tests"


def _load_backend_conftest() -> types.ModuleType:
    """Load `backend/tests/conftest.py` under a name of its OWN.

    A plain `import conftest` cannot work from here: pytest has already
    registered THIS file as the module named `conftest`, so the import would
    hand back this half-built module and fail on the first name taken out of
    it. Loading the backend's file from its path under an explicit alias
    sidesteps the collision entirely.

    Importing it also runs its module body, which registers and loads the
    derandomized hypothesis profile (D-gate determinism, backend conftest) —
    which is what keeps the checksum cross-check green or red for a reason
    rather than by luck.

    `backend/tests` goes on `sys.path` as well, so a sim test can reach the
    backend's other shared test helpers the same way its own suites do.
    """
    if str(BACKEND_TESTS) not in sys.path:
        sys.path.insert(0, str(BACKEND_TESTS))
    spec = importlib.util.spec_from_file_location(
        "eoe_backend_conftest", BACKEND_TESTS / "conftest.py"
    )
    assert spec is not None and spec.loader is not None, f"no conftest at {BACKEND_TESTS}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_backend = _load_backend_conftest()

# One broker fixture, one Postgres fixture, one repository.
docker_retry = _backend.docker_retry
ephemeral_broker = _backend.ephemeral_broker
ephemeral_postgres = _backend.ephemeral_postgres
free_port = _backend.free_port
make_kek = _backend.make_kek


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items) -> None:
    """Pin every test to an xdist group named after its module (D99).

    **`tryfirst` is load-bearing.** xdist reads the `xdist_group` mark inside
    its OWN `pytest_collection_modifyitems` and bakes the group into the
    nodeid there; a mark added after that hook has run is never seen, and
    every test scatters across workers as if unmarked. The failure is silent —
    the suite still runs, it just runs wrong — and it cost the backend a red
    gate to find.
    """
    for item in items:
        module = item.module.__name__.rsplit(".", 1)[-1] if item.module else "orphan"
        item.add_marker(pytest.mark.xdist_group(module))


@pytest.fixture
def anyio_backend() -> str:
    """The harness is asyncio (aiomqtt, the phase's fixed client choice), so
    async tests run on anyio's plugin pinned to the one backend the code
    actually uses. Parametrizing over trio as well would double the suite for
    a runtime nothing here will ever run on."""
    return "asyncio"
