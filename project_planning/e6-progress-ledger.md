# E6 Progress Ledger

Per-task state for epic E6 (Map and monitoring), kept current as the epic is built. The phase
document (`phase-6-map-monitoring.md`) is the binding scope. This file records where the work
stands, so a session joining mid-epic does not have to piece it together from git.

**Rule R0 governs the order.** A task is `gate green` only when `make gate` passed the ENTIRE
accumulated suite: 0 failed, 0 skipped, 0 xfailed, 0 deselected. No task starts before the
previous one is tagged. The agent implementing a task fills in its row, and writes the tag
column only after the commit and tag exist.

**Gate numbers are decided at tag time, not pencilled in** (the E4/E5 convention, D160). Each
task takes `max(existing gate-* tags) + 1` when its tag is written and records that number
here.

**Task order is not numeric order.** E6.4 runs before E6.2b, because markers cannot be colored
before the rollup exists (project-changes #47). The rows below are in working order.

| Task | Status | Gate | Tag | Decisions | Notes |
|---|---|---|---|---|---|
| E6.0 Phase document and records | **done (no gate of its own, D192)** | covered by 81 | gate-81 | D188-D192 | Phase doc, this ledger, project-changes #47-#49, addenda PLAN-3-05 / SPEC-9-01 / DES-7-04. Docs only: `test_governance` 14 passed on its own; committed locally and pushed with E6.1's gate. |
| E6.1 Map foundation | **gate green** | 81 | gate-81 | D188, D193 | `app/map/basemaps.py` + `GET /map/config` (`require_any_assignment`); `EOE_MAP_*` settings (blank = unset; image pair validated fail-loud). `lib/basemaps.ts` (closed ids, one factory each; Google via Map Tiles API session + per-viewport copyright; PMTiles raster/vector, lazy chunks), `components/map/MapView.tsx`, `Map.tsx` (EmptyState retired). Leaflet controls relit from tokens. Tests: `test_map_config.py` 16, `basemaps.test.ts` 14, `map-page.test.tsx` 5, `e2e/map.spec.ts` 2 (no tile host contacted). E1 walkthrough `/map` line amended. Gate 81 green on run 3 (run 1: unpinned compose vars, fixed; run 2: Docker forwarder 500, D181). Manual: real Esri and OSM tiles loaded and screenshotted. |
| E6.2a Coordinates for Aggregators and Deployments | not started | | | D189 | |
| E6.4 Status rollup engine and map endpoints | not started | | | D190, D191 | |
| E6.2b Hierarchy markers and clustering | not started | | | | |
| E6.3 Leaf versus pin logic | not started | | | | |
| E6.5 Device detail drawer | not started | | | | |
| E6.6 Live map updates | not started | | | | |
| E6.7 Owner landing view | not started | | | | |
| E6.8 Walkthrough and SIM acceptance | not started | | | | |

Batches: one PR per batch, on `e6-batch-N` (rule R3, project-changes #47).

| Batch | Tasks | Checkpoint |
|---|---|---|
| **B1** | E6.0-E6.2a | CP1 |
| **B2** | E6.4, E6.2b, E6.3 | CP2 |
| **B3** | E6.5, E6.6 | CP3 |
| **B4** | E6.7, E6.8 | CP4 |

**Each batch is branched from `main` only after the previous batch's PR has merged.** Every E6
batch changes the UI, so each PR waits for the owner's hands-on check before it is merged.
Phase document §7 has the checklist for each checkpoint.

## Owner checkpoints

| Checkpoint | Stack up | Owner result | Follow-ups |
|---|---|---|---|
| CP1 | | | |
| CP2 | | | |
| CP3 | | | |
| CP4 | | | |
