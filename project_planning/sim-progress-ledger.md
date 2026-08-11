# SIM Progress Ledger

Per-task state for epic SIM, maintained as the epic is implemented. The phase document
(`phase-sim-simulation-harness.md`) is the binding scope; this file is the running record of
where the work is, so a session joining mid-epic does not have to reconstruct it from git.

**Rule R0 governs the order.** A task is `gate green` only when `make gate` passed the ENTIRE
accumulated suite — 0 failed, 0 skipped, 0 xfailed, 0 deselected. No task starts before the
previous one is tagged. The implementing agent fills in its own row; the tag column is written
only after the commit and tag exist.

| Task | Status | Gate | Tag | Decisions | Notes |
|---|---|---|---|---|---|
| SIM.0 Phase document and records | gate green | 52 | `gate-52` | D97, D98 | Phase doc, this ledger, project-changes #23, addendum PLAN-3-02. Not docs-only in the end: the gate exposed a flaky E3.2 shutdown assertion (D97) and gained a per-test timeout (D98). |
| Gate speed-up (not a SIM task) | gate green | 53 | `gate-53` | D99 | Parallel backend suite, module-grouped. 541s → 260s. Taken before SIM.1 because five gates still had to be paid for. |
| SIM.1 Mock Aggregator | gate green | 54 | — | D100, D101, D102, D103 | `/sim` uv project (own venv, ruff/mypy matching the backend), `sim/checksum.py`, `sim/device.py` (`BrokerLogin`, `MockAggregator`), `sim/tests/` bridging to the backend fixtures. All four acceptance claims asserted against a real broker and a real platform. |
| SIM.2 Mock Listener behaviour | not started | 55 | — | — | `MockListener`, spec 6.5 liveness, wake declarations over the modelled local link. |
| SIM.3 Scenario scripting | not started | 56 | — | — | Behaviour registry + TOML files; six catalogued scenarios, each asserting the platform's reaction. |
| SIM.4 Fleet runner | not started | 57 | — | — | CLI, REST provisioning, `sim` compose profile, 20 × 30 default. |
| SIM.5 CI integration | not started | 58 | — | — | `sim-quality` + `sim-protocol` stages, `guide/sim-verification.md`, INTERFACES section. |

## Notes for whoever picks this up next

- The published contract (`backend/app/contracts/mqtt.py`) is **additive-change-only**. If a task
  finds it genuinely insufficient, that is a stop-and-ask, not an edit.
- `backend/tests/conftest.py` owns the only Mosquitto fixture in the repository. `/sim`'s tests
  bridge to it; they do not grow a second one.
- Scale is a parameter everywhere. CI runs 2 × 3; the documented load check runs 20 × 30.

## Notes from SIM.1, for SIM.2 onward

- **`/sim` is its own uv project** with its own `.venv` (`cd sim && uv run ...`). The suite is not
  in `gate.sh` yet — SIM.5 adds `sim-quality` (`uv run ruff check . && uv run ruff format --check
  . && uv run mypy`) and `sim-protocol` (`uv run pytest`). Until then, run both by hand beside
  `make gate`. `sim/tests/conftest.py` deliberately does NOT carry the backend's EOE_GATE
  `pytest_sessionfinish` guard; SIM.5 owns wiring the R0 enforcement for this suite.
- **mypy is configured through `files`**, so the invocation is a bare `uv run mypy` (harness
  modules only — the suite is excluded, as the backend excludes its own).
- **The device surface SIM.2 extends:** `MockAggregator` subscribes to its `desired` and `cmd`
  topics only. The Listener subtopics (`lst/{mac}/desired`) are deliberately unsubscribed rather
  than half-handled; `_deliver`'s `match` has the branch shape SIM.2 slots into, and the
  waiters (`wait_for_apply`, `wait_for_command`) share one `_progress` event that a Listener
  waiter can reuse.
- **The published contract was sufficient.** Nothing in `app.contracts.mqtt` turned out to be
  missing for an Aggregator; no stop-and-ask was needed and the module is unchanged.
- **A pre-existing platform cost, NOT introduced here and not fixed here (rule R2):** each
  `POST /config/apply` publish takes ~10s to be confirmed, so an aggregator-level change over
  the seeded hierarchy (1 aggregator + 8 listener revisions) costs ~90s. Confirmed as the same
  behaviour in `backend/tests/test_end_to_end_loop.py`, which is where it belongs — the
  confirmation wait is in E3.2/E3.4, not in `/sim`. Worth raising with the platform owner
  before SIM.4 puts a 20 × 30 fleet through the same path.
