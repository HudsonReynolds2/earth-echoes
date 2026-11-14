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
| E6.2a Coordinates for Aggregators and Deployments | **gate green** | 82 | gate-82 | D189, D194 | Migration `670aec9e9363` (columns + `gps_pair`/`gps_range` CHECKs on both tables). API: `CoordinatesIn`, `apply_coordinates` (PATCH judged by result), outputs carry gps; Pod-create inline aggregator. Aggregator import: optional `gps_lat,gps_lon` tail. Seed + `inventory-fixture.ts` placed (Orick CA / Fort Rock OR; listeners ~130 m around their Pod). SIM `FleetPlan.deployment_coords/pod_coords/listener_coords`. `LocationCard` on deployment and pod pages. Tests: `test_map_coordinates.py` 23, `test_seed_demo` amended, SIM `test_a_fleet_is_placed_on_the_map_deterministically`, `location.test.tsx` 8. Gate 82 green on run 2 (run 1: migration not ruff-formatted). Manual: live API on QA project `eoe-e6`. Owner CP1 passed. |
| E6.4 Status rollup engine and map endpoints | **gate green** | 83 | gate-83 | D190, D191, D195 | `device_status.with_alerts` (applied in `hierarchy_common`'s readers: inventory and map agree). `app/map/alerts.py` (the E7 seam), `rollup.py` (pure), `service.py`; `GET /map/deployments`, `GET /map/deployments/{id}` (404 out of scope). `STATUS_SEVERITY`/`worstStatus` pinned to the backend. `test_map_endpoints.py` 48 (incl. constant query count at 1 and 1,000 Listeners), `status-severity.test.ts` 2. E3 walkthrough's two `alerting` lines amended. Gate 83 green on its first complete run. |
| E6.2b Hierarchy markers and clustering | **gate green** | 84 | gate-84 | D196, D197 | Routes `/map`, `/map/deployments/:id`, `/map/pods/:id`; `?device=listener:<MAC>`. `markers.ts` (pure, escaped), `MarkerLayer.tsx` (keyed, in-place; `FitToPoints` once per level), `UnplacedPanel.tsx`, `leafletGlobal.ts`. Lenses (D196 deviation: status-based). Interim selected-Listener card until E6.5. Tests: `map-markers.test.ts` 11, `map-drill.test.tsx` 11, `e2e/map.spec.ts` +2 (drill, cluster→pin, keyboard). Gate 84 green on run 2 (run 1: Docker forwarder 500, and a SIM precondition race, D197). |
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
| CP1 | `eoe-e6` (fresh; `eoe-qa` kept, credentials mismatch) | **passed** 2026-11-14 ("It all looks great"; satellite and street "work great") | Login first failed: the July `deploy/.env` still had `EOE_CORS_ORIGINS=http://localhost:5173`; corrected to 15173. PR #26 merged after CI went green on its third run (D195). |
| CP2 | | | |
| CP3 | | | |
| CP4 | | | |
