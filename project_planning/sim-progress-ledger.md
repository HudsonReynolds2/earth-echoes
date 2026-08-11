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
| SIM.1 Mock Aggregator | not started | 54 | — | — | `/sim` project skeleton, `sim/checksum.py`, `MockAggregator`. |
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
