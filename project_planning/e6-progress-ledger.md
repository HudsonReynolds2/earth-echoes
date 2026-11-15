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
| E6.3 Leaf versus pin logic | **gate green** | 85 | gate-85 | D198 | `leafFan.ts` (pure, upper half), `LeafFan.tsx` (pixel offsets projected per zoom; focused Pod, or zoom ≥ 14), leaf markup with CSS tethers, *Show GPS-less* toggle (remembered), `<n> no GPS` in the Pod label, unplaced Pods count GPS-less Listeners. Tests: `leaf-fan.test.ts` 8, `map-leaves.test.tsx` 4 (incl. leaf → pin on refetch), `map-markers.test.ts` +1, `e2e/map.spec.ts` +1 (polls nearness). Visual check by screenshot found and fixed the label overlap. Gate 85 green on its first run. |
| H1 Hygiene (owner-approved, project-changes #50) | **gate green** | 86 | gate-86 | D199 | Listener API + import refuse half a GPS pair (D194 rule); migration `1d73e4435c21` adds `ck_listener_gps_pair`/`gps_range`. `ephemeral_postgres` re-acquires a vanished template once (D195). SIM LWT test polls its precondition (D197). Tests: `test_map_coordinates.py` +9 (5 API/import, 4 DB), `test_container_pool.py` +2. Branch `e6-hygiene`, stacked on `e6-batch-2`. Gate 86 green on its first run. Manual: migration applied to `eoe-e6`; psql shows both CHECKs refusing. |
| E6.5 Device detail drawer | **gate green** | 87 | gate-87 | D200 | `GET /map/devices/{entity_type}/{id}` (`MapDeviceOut`, one shape, four kinds; status from the marker's readers); `GET /provisioning/records?target_type=&target_id=`. `DeviceDrawer.tsx` (five tabs, `panelsFor`), `LeaderLine.tsx` (`leaderSegment`, `data-marker-key`), `?device=`/`?panel=`, Details control, halo on pods/deployments, `--eoe-width-drawer`. Tests: `test_map_devices.py` 10, `map-drawer.test.tsx` 17, `map-drill.test.tsx` deep-link amended, `e2e/map.spec.ts` +1 (leader line geometry, Esc focus). Gate 87 green on run 2 (run 1: `E0_ROUTES` not yet extended; Docker forwarder 500, D181). Manual: screenshots in both themes found and fixed three defects. |
| E6.6 Live map updates | **gate green** | 88 | gate-88 | D201 | `useLiveUpdates` invalidates the map on `device_status` and `reconciliation` through `createCoalescer` (2 s, leading + trailing; `isLiveMapQuery` skips `["map","config"]`). `MAP_POLL_MS` 30 s on the layer, detail and device reads (interim for services/alerts). Import and services refresh now invalidate the map. Tests: `map-live.test.tsx` 6 (one refetch, in place, view and drawer unchanged; 200 events, one per window; config untouched), `test_map_live.py` 1 (the disconnect LWT carries the Deployment `/ws` filters on). Gate 88 green on its first run. Manual: live SIM `disconnect` turned SIM Pod 000 Offline in headless Chromium with no reload. |
| E6.9 Unreachable Listeners (project-changes #52, #53) | **gate green** | 89 | gate-89 | D202, D203 | `UNREACHABLE` + `behind_aggregator` in `device_status`; applied in `listener_statuses` (one more constant query). `SEVERITY` places it below `offline`. Frontend vocabulary, tokens (both themes), glyph subset re-cut with U+2B21, drawer note. Tests: `test_unreachable.py` (pure + inventory/map/drawer agreement), `map-drawer.test.tsx` +1, legend test now seven, E3 walkthrough amended. Gate 89 green on its first run. Manual: screenshot shows the hexagon rendering, seven-status legend and drawer note. |
| E6.7 Owner landing view | **gate green** | 90 | gate-90 | D204 | `Overview.tsx` rebuilt on the map reads: hero online/registered (`onlineTotals`; unreachable not online), a card per Deployment (rollup, distribution, E5.5 service chips, Pods, Open map), Needs attention (`attentionItems`: worst first, cut-off Listeners folded per Pod, capped at 8 with `?tab=` links), E7 telemetry line. Map reads `?tab=`. Tests: `overview.test.tsx` 11 (E1 tests amended, D204). Gate 90 green on run 4 (run 1: a loaded-runner timeout, given headroom; run 3: Docker forwarder). Manual: real-data Overview with a SIM disconnect read 4 / 42 and folded the cut-off Listeners. |
| E6.8 Walkthrough and SIM acceptance | **gate green** | 91 | gate-91 | D205 | `sim/tests/test_map_fleet.py` (demo fleet as mock devices: healthy rollup with every Pod placed, `disconnect` → Pod/Deployment offline and Listeners unreachable, `missed_wake_window` → Listener offline with its Aggregator healthy; ~33 s). `guide/e6-verification.md`, indexed; E1 §1 and the not-yet-filled surfaces list amended, E3 status section amended at E6.9. Clean-stack dry run (`eoe-walk`, removed) found and fixed three run-sheet defects. Gate 91 green on run 2 (run 1: Docker forwarder, D181). |

Batches: one PR per batch, on `e6-batch-N` (rule R3, project-changes #47).

| Batch | Tasks | Checkpoint |
|---|---|---|
| **B1** | E6.0-E6.2a | CP1 |
| **B2** | E6.4, E6.2b, E6.3 | CP2 (held with CP3, project-changes #51) |
| **B3** | E6.5, E6.6 | CP3 |
| **B4** | E6.9, E6.7, E6.8 | CP4 |

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

## Resume here (rewritten 2026-11-15, after batch 4)

**State.** Every E6 task is built and gated: batch 4 (E6.9 gate 89, E6.7 gate 90, E6.8 gate
91) is pushed on `e6-batch-4` with its PR open against `main`. Batches 1-3 and H1 are merged.
**The batch waits for the owner's CP4**, which runs `guide/e6-verification.md` start to
finish. Merge on approval after CI is green, and that closes the epic.

**Next, in order:**

1. **CP4 with the owner.** Bring the QA stack up (below), re-mint the broker accounts, wait
   for the API, run the SIM fleet, and give the owner phase-6 §7's CP4 plus the walkthrough.
2. **Merge on approval.** Then write the epic-close entry and offer the next epic.

**QA stack on this WSL host** (`qa-stack.ps1` is Windows-only):

- **Project.** It runs as compose project **`eoe-e6`**, not `eoe-qa`. The `eoe-qa` database
  volume predates this checkout's `deploy/.env` credentials and is kept untouched (see the
  E6.2a project-updates note).
- **`deploy/.env`** (local, gitignored) gained `EOE_FIRMWARE_KEK` and had `EOE_CORS_ORIGINS`
  corrected from 5173 to 15173 during CP1. Without the second, the browser login fails with
  "NetworkError".
- **Up:**

  ```bash
  set -a; . deploy/.env; set +a
  (cd backend && uv run python -m app.devbroker --certs-only)
  docker compose -f deploy/docker-compose.yml -p eoe-e6 --env-file deploy/.env up -d --build --wait
  export DATABASE_URL="${DATABASE_URL/@postgres:5432/@localhost:15432}"
  (cd backend && uv run python -m app.devbroker --host mosquitto --keep-tls)
  docker compose -f deploy/docker-compose.yml -p eoe-e6 --env-file deploy/.env restart mosquitto
  (cd backend && uv run alembic upgrade head)   # the stack does not migrate on start
  ```

  The demo is already seeded in `eoe-e6`. The owner password was printed once, at seed time
  in the session chat, and is deliberately recorded nowhere in the repository. If it is lost,
  `down -v` the `eoe-e6` project and seed again with `uv run python -m app.seed --demo`.
- **The SIM fleet for CP2:** follow `guide/sim-verification.md` §3-§5 with
  `-p eoe-e6 --env-file deploy/.env` on every compose command. Since E6.2a the fleet's
  Deployment, Pods and half its Listeners have coordinates, so it draws on the map. The
  `disconnect` and `missed_wake_window` scenarios are CP2's status checks.
- **Down before any gate** (D44, same host ports):
  `docker compose -f deploy/docker-compose.yml -p eoe-e6 --env-file deploy/.env down`
  (keeps volumes).

**Known flakes on record:** D99/D181 Docker forwarder 500s (rerun after three forwarding
probes pass), D195's CI keystone Grafana port (D165). D195's pool template and D197's SIM precondition
race are fixed by H1 (D199).
