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
| SIM.1 Mock Aggregator | gate green | 55 | `gate-55` | D100, D101, D102, D103 | `/sim` uv project (own venv, ruff/mypy matching the backend), `sim/checksum.py`, `sim/device.py` (`BrokerLogin`, `MockAggregator`), `sim/tests/` bridging to the backend fixtures. All four acceptance claims asserted against a real broker and a real platform. **Gated with SIM.2 and SIM.3** (project-changes #24, addendum PHASESIM-4-01). |
| SIM.2 Mock Listener behaviour | gate green | 55 | `gate-55` | D104, D105 | `MockListener` with no session of its own, the local link as an in-process call that refuses what a Listener cannot mean (D104), and the spec 6.5 sweep running in the device on its own configured grace (D105). All four acceptance claims asserted. |
| SIM.3 Scenario scripting | gate green | 55 | `gate-55` | D106, D107, D108 | Typed behaviour registry + six TOML scenario files, each with a test asserting the PLATFORM's reaction (`failed`, `drifted`, LWT offline, Listener offline, `duplicate_identity` quarantine with inventory unchanged, `provisioning_required`). Load-time validation naming file and key (D107). Found and fixed D108 — an in-process kill took the event loop down. |
| Concurrency-safe test infrastructure (not a SIM task) | gate green | 56 | `gate-56` | D110 | Cross-process lock, machine-wide port-claim registry, host-side forward probes. Taken on the owner's instruction after two gate-55 runs were lost to a concurrent suite in another worktree. Two full backend suites now run at once, 766 passed each. project-changes #25, addendum PHASE0-4-07. |
| SIM.4 Fleet runner | gate green | 58 | `gate-58` | D112, D113, D114, D115 | `sim/provision.py` (REST `Operator`, `FleetPlan`, `mint_credentials`), `sim/fleet.py` (CLI, `Fleet`, counters, staggered start, polite shutdown), `sim/Dockerfile` and the `sim` compose service behind its profile, `COMPOSE_SERVICES`/`PROFILED_SERVICES` extended. **Gated with SIM.5** (see the note below). The 20 × 30 load run is measured and recorded in `guide/sim-verification.md`. |
| SIM.5 CI integration | gate green | 58 | `gate-58` | — | `sim-quality` + `sim-protocol` in `gate.sh` (and `gate.ps1`), a CI job each with both ids in `ci-green`, `sim/tests/gate_runner.py` reusing the backend's `GateGuard`/`enforce`, the EOE_GATE hook in sim's conftest, `guide/sim-verification.md`, the INTERFACES "Owned by SIM" section. |

## Notes from SIM.4 and SIM.5 — the epic is complete

- **SIM.4 and SIM.5 share gate 58** (project-changes #26, addendum PHASESIM-4-02), superseding
  PHASESIM-4-01's "SIM.4 and SIM.5 gate individually". SIM.5 is what puts `/sim` into `gate.sh`,
  so the folded gate strictly contains what a SIM.4-only gate could have asserted. Gate 57 went
  to the concurrent E5 batch on another branch, so the numbers this ledger reserved shifted by
  one — R3 forbids moving a tagged gate commit.
- **`/sim` is in the gate now.** `sh gate.sh sim-quality` and `sh gate.sh sim-protocol`, both in
  `LOCAL_STAGES`, both in `gate.ps1`, both with a CI job in the `ci-green` needs list. No more
  running the harness suite by hand beside `make gate`.
- **The 20 × 30 run is real and it is fast**: 3.0s to provision over REST, 2.1s to connect 20 TLS
  sessions, 5.2s for 620 devices to converge, 45 MiB peak RSS. The load bottleneck is the WORKER
  (~60% of one core), not the broker (5 MiB, 0.03%). Numbers and procedure in
  `guide/sim-verification.md` section 6, which is what E8.6 inherits.
- **The ~10s-per-publish cost the SIM.1 notes flagged does not exist.** It was a
  `fastapi.testclient` artefact; over real HTTP the publish path is fast (D115). That note is
  superseded — nothing needs raising with the platform owner.
- **Two defects the load run found, both in the harness** (D114, D115): a `KeyError` where a
  stale pre-E3.13 API image should have been named, and a runner that waited for 620 devices after
  an apply that legitimately published nothing. Both fixed, both tested. The pattern from D108
  holds: the harness's failures are not in the protocol, they are in what it does when the world
  is not fresh.
- **What E6 and E8 drive:** `fleet.py --scenario NAME --scenario-devices N --stay` for a fleet
  where some devices are wrong and the rest are healthy; `docker compose --profile sim up sim` for
  one against the dev stack. `duplicate_mac` and `unprovisioned_aggregator` stay suite-only and
  are refused at startup with the reason (D113) — that answers the question SIM.3 left open.

## Notes for whoever picks this up next

- The published contract (`backend/app/contracts/mqtt.py`) is **additive-change-only**. If a task
  finds it genuinely insufficient, that is a stop-and-ask, not an edit.
- `backend/tests/conftest.py` owns the only Mosquitto fixture in the repository. `/sim`'s tests
  bridge to it; they do not grow a second one.
- Scale is a parameter everywhere. CI runs 2 × 3; the documented load check runs 20 × 30.

## Notes from SIM.2 and SIM.3, for SIM.4 onward

- **The device surface SIM.4 drives.** `MockAggregator.add_listener(mac)` attaches a Listener and
  subscribes to its desired subtopic on a RUNNING device, so a fleet does not have to know its
  Listeners before it connects; `listener(mac)` and `first_listener()` fetch them. Lifecycle is
  `connect()` / `disconnect()` (polite, publishes `offline`) / `kill()` (ungraceful, leaves the
  will to the broker) — SIM.4's "shutdown is clean" is therefore already true of the device, and
  `kill()` is the only thing that should ever look like a crash.
- **Counters SIM.4's reporting can read without adding any:** `published_reports` on both the
  Aggregator and each Listener, `applied_revision_ids` (the whole history, not just the latest),
  and `commands_executed`.
- **Run a scenario with `load_scenarios()` + `Scenario.run(ScenarioContext(aggregator=...))`.**
  `ScenarioContext` is a frozen dataclass with one field precisely so SIM.4 can hand behaviours
  more of the fleet — counters, a clock, sibling devices — without changing the signature of
  every behaviour that never needed it. `--scenario` should call `load_scenarios()` at STARTUP
  and fail there: that is D107's whole point, and a runner that loaded lazily would give it away.
- **The `duplicate_mac` and `unprovisioned_aggregator` scenarios need a device the ordinary fleet
  would not create** — one whose `aggregator_uuid` is in no inventory row, and one willing to
  claim another parent's MAC. The suite builds these with the devbroker credential of a
  legitimately provisioned aggregator plus a different `aggregator_uuid`; see
  `tests/test_scenario_outcomes.py`. SIM.4 will need to decide whether the CLI exposes that at
  all, or whether those two scenarios stay suite-only.
- **The wake sweep is per-device and runs every 0.5s** (`wake_sweep_interval`, a constructor
  parameter). At 20 × 30 that is 20 sweeps per half-second over 30 Listeners each; it is a list
  scan over in-memory objects with no I/O unless something is actually overdue, but it is the
  one piece of per-device background work in the harness and the first thing to look at if a
  full-scale run shows unexplained CPU.
- **D108 is the shape of bug to expect at scale.** The harness's failures so far have not been in
  the protocol; they have been in what asyncio does to a device that dies. A 20 × 30 run kills
  and reconnects far more sockets than the suite does.

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
