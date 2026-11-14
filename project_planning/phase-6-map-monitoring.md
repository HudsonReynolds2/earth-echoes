# Phase 6 Document: Map and Monitoring (Epic E6)

**Companion documents:** Technical Specification v1.1 (authoritative), Project Development Plan v1.0
**Spec sections implemented:** 9 (all of it), the spec 16.5 map-rollup sentence, and the
non-telemetry half of spec 10.3's Owner view
**Depends on:** E0, E1, E2, E3, E4, E5 and SIM complete and merged (gates 0-80). The plan's
dependency line, "E1, E3 (E5 for services status)", is exceeded: every epic E6 reads from has
shipped.

---

## 1. Scope

Build the map and the monitoring surfaces around it. That covers the map engine and its
switchable basemaps, coordinates for the two levels that have none today, the server-side
status rollup that every marker and card reads, the hierarchy markers with clustering, the
leaf-versus-pin rule for GPS-less Listeners, the device detail drawer, live updates without
reload, and the Owner landing view.

When this phase ends, an operator opens **Map** and sees satellite imagery with one marker per
Deployment, colored, shaped and labelled by the worst status beneath it. Clicking a Deployment
drills to its Pods. Clicking a Pod shows its Listeners: pins where they have GPS, leaves on a
short tether where they do not. Clicking any device opens a drawer with its status, effective
config, reconciliation timeline and provisioning card. When a device goes offline in the
field, its marker changes within seconds, with no reload and without losing the operator's
zoom or open drawer. The Overview page shows how many devices are online out of how many are
registered, and one card per Deployment.

**E6 shows what the platform already knows. It adds no new signal.** Every status a marker
shows comes from E3's derivation (`controlplane/device_status.py`), E5's `services_status`, or
E1's open `inventory_alert` rows. If a marker needs a fact that is not in Postgres, the fact
belongs to another epic (telemetry and Grafana alerts are E7), and the marker does not invent
it. This is D40/D60's honesty rule carried onto the map: a device that has never spoken shows
**no status**, not a healthy one.

## 2. Prerequisites and inherited interfaces

Read `docs/INTERFACES.md` first. What this phase consumes, and does not redefine:

**`controlplane/device_status.py`** (E3.12, D60). This module is **the** spec 9.3 derivation:
`aggregator_status`, `listener_status`, `rollup`, `SEVERITY`. Severity is worst first:
alerting, offline, degraded, drifted, sleeping, healthy, unknown. `unknown` ranks below
everything, so one silent device does not make a Deployment read as unknown. The inventory
tables, the aggregator card, and now every marker read this one module. **E6 does not write a
second derivation**, on either the server or the client. The client receives each marker's
final status from the API. Its one local rollup is the cluster icon (E6.2b), and that uses the
same severity order, pinned against the server's by a test.

**`api/hierarchy_common.py`**: `aggregator_statuses`, `listener_statuses` and
`_latest_revision_states`. These are the batched readers (two queries, not 2N) that E6.4's map
endpoints build on.

**`aggregator_status` (E3.8)** gives the LWT online verdict. **`device_state` liveness (E3.9)**
gives `liveness_state`, `last_audio_at`, `expected_wake_at` and `liveness_changed_at`. The
drawer's Status tab shows these as they are.

**`deployment.services_status` (E5.1/E5.5).** `unconfigured`, `pending_verification`,
`verified` or `degraded`, with **E5.5 as its only writer**. E6 reads it and never writes it.
`GET /deployments/{id}/services/status` (E5.5) feeds the drawer and Overview service chips.

**`inventory_alert` (E1.5).** Open rows (`resolved_at IS NULL`) of type `duplicate_identity`
(on a listener MAC) or `provisioning_required` (on an `aggregator_uuid` that may not exist in
inventory), scoped by un-FK'd `deployment_id`. Until E7, these are the only alerts that exist.

**`WS /ws` and `lib/useLiveUpdates.ts` (E3.12).** The live bus is Postgres `LISTEN`/`NOTIFY`.
Events are **invalidation signals, never data**, and scoping is applied on the server for
each event. The channels are `device_status` and `reconciliation`, and E7 adds `alerts`. The
hook is mounted once in `Shell` and invalidates query keys. E6.6 adds the map's keys to it.

**`timeline` (E3.11), `config/effective` (E2.4), `POST /aggregators/{id}/commands`
(E3.10), and the provisioning records (E4.9).** The drawer's tabs read these existing
endpoints. `components/DeviceTimeline.tsx`, `ProvenanceTable.tsx` and `RecordChip.tsx` are
reused, not re-built.

**The demo fixture (E1.9, D43)** names two Deployments, six Pods and 28 Listeners. Even-index
Listeners carry GPS at `(47.6 + i/100, −121.88 − i/100)`, which is **the same spot for listener
*i* in every Pod**, so those pins would sit on top of each other across Pods and Deployments.
E6.2a fixes this (section 2's authorized edits). `frontend/tests/inventory-fixture.ts` mirrors
the fixture and moves with it.

**The SIM fleet (SIM.1-SIM.5).** `sim/provision.py` creates every Listener with
`gps_lat: None, gps_lon: None` and gives Aggregators no coordinates. Scenarios
(`disconnect`, `missed_wake_window`, `drift`, `apply_error`, `duplicate_mac`) are the
interface E6 drives rather than rebuilds.

**Frontend (E0.4, DES.7, project-changes #9).** `pages/Map.tsx` is the reserved frame:
`ContextBar` with the Status / Reconciliation / Alerts tabs, the `map-region`, and the real
`StatusLegend`. `StatusChip`/`StatusCell`/`DEVICE_STATUSES` are the closed six-state vocabulary.
**Status is three channels: color, glyph and label.** The glyph comes from
`--eoe-color-status-{name}-glyph` and renders in the vendored glyph font (D27). Layout comes
from **S2 in `Screens.dc.html`**, "Map tab — pod drill-down with device detail drawer", labelled
E6.1-E6.6 · spec §9. Values and the drawer treatment come from **V2 "Map with the device drawer
open"** in `Screens v2.dc.html`: the map dims behind the drawer, the selected marker keeps a
halo, a leader line runs from marker to drawer, the drawer has a deep shadow and a 3px accent
edge, and its header has a title, a Close button and an Esc hint. The Owner view comes from
**V2·S1**. **v2 wins on every value** (DES handoff).

**RBAC (E0.7).** `VIEW_STATUS` is held by all four roles. It gates the map and its data, scoped
through `visible_deployments(..., VIEW_STATUS)` exactly as `/ws` is. Each drawer tab is gated
by the permission its source endpoint already enforces. **This phase adds no permission.**

### Fixed choices for this phase

The owner decided these three at plan approval (2026-11-13). **They are not to be
relitigated mid-epic.** If a task appears to need one reopened, stop and ask.

1. **Leaflet engine with switchable basemaps; satellite by default.** Leaflet with
   react-leaflet, as spec 9.1 says. The tile source sits behind the interface spec 14.5
   requires (`frontend/src/lib/basemaps.ts`, a closed `BasemapSource` union with one
   layer factory). The sources are:

   | id | When offered | Notes |
   |---|---|---|
   | `esri_imagery` | online (default) | Esri World Imagery satellite tiles. No key. Attribution required. |
   | `osm` | online | OpenStreetMap standard tiles. Attribution required. |
   | `google_satellite` | only when `EOE_MAP_GOOGLE_API_KEY` is set | Google Map Tiles API 2D satellite. |
   | `pmtiles` | when `EOE_MAP_PMTILES_URL` is set | Single-file basemap for air-gapped hosts (spec 9.1, 15.1). |
   | `image` | when `EOE_MAP_IMAGE_URL` and `EOE_MAP_IMAGE_BOUNDS` are set | An operator-supplied georeferenced image. This is the #9 direction, kept as an offline option. |
   | `none` | always | Plain background. Used by tests, and as the fallback when offline with nothing configured. |

   - **Offline mode.** `EOE_MAP_OFFLINE=true` removes every online source, and the first
     configured offline source becomes the default.
   - **How the browser gets the configuration.** The API serves it at runtime from
     `GET /api/v1/map/config` (`VIEW_STATUS`), so an operator changes basemaps without
     rebuilding the frontend (the frontend is fully decoupled, D2). The browser remembers the
     chosen basemap per viewer in `localStorage`, wrapped in try/catch.
   - **Attribution.** It is always visible and comes from the source definition, never from a
     literal string in a component.
   - **The Google key is a browser key, not a secret.** It necessarily reaches the browser,
     so it does not go through `SecretStore`. It must be HTTP-referrer-restricted, and
     `.env.example` and the README say so by name.
   - **E6.1 stops and asks** if Google's current terms forbid showing Map Tiles API tiles in a
     non-Google renderer. In that case the source is dropped, and the drop is recorded rather
     than worked around.
   - **This supersedes project-changes #9's "Google Maps JS API" direction** and changes spec
     9.1's default from OSM to satellite. Both are recorded in project-changes #48 and #49,
     spec addendum SPEC-9-01, and DECISIONS D188.
   - **Licensing remainder.** Esri's terms for production use of World Imagery (attribution
     and account requirements) are confirmed before a production deploy. E8.7's dependency
     and security review owns that check, and D188 records it as open.

2. **Aggregators and Deployments get coordinates, with no derived fallback.**
   - **New columns.** `gps_lat`/`gps_lon` (nullable `Float`) are added to `aggregator` and
     `deployment` by an additive, reversible migration. They are validated like the Listener's
     columns (`ge=-90, le=90` / `ge=-180, le=180`). **Both-or-neither** is enforced by a named
     CHECK constraint and at the API, following E4.11's "half coordinates refused".
   - **What each marker uses.** A Pod's marker sits **at its Aggregator's coordinates** (spec
     9.2). A Deployment's marker sits at its own coordinates.
   - **No fallback.** There is no centroid or other derived fallback, because the system has
     no field data that would need one (owner, 2026-11-13). Anything without coordinates is
     not drawn. It is listed in an **Unplaced** panel beside the map, with a link to the
     inventory form that fixes it (`manage_devices`). Listeners keep spec 9.2's leaf rule
     (E6.3), which is not a fallback: a leaf is drawn as visibly not a real coordinate.
   - **Fixture and fleet.** The demo fixture and the SIM fleet get deterministic coordinates
     for all three levels (E6.2a).
   - DECISIONS D189.

3. **Only `services_status = 'degraded'` changes a Deployment marker's status.** A Deployment's
   rolled-up status is `rollup(all descendant device statuses + [DEGRADED if services_status ==
   'degraded'])`.
   - `unconfigured` and `pending_verification` do not change device status. They render as a
     separate services chip on the marker label, the drawer and the Overview card, so a
     brand-new Deployment is not painted as broken.
   - `verified` adds nothing.
   - DECISIONS D190.

These structural choices are this document's own, not the owner's:

4. **Alerts in E6 are the open `inventory_alert` rows, attached where they can be.**
   - A `duplicate_identity` alert on a MAC in inventory attaches to that Listener. A
     `provisioning_required` alert on an `aggregator_uuid` in inventory attaches to that
     Aggregator.
   - An attached alert makes the device **`alerting`** (the top of `SEVERITY`) and gives it an
     **alert badge with its count**.
   - An alert that matches no inventory row counts toward its Deployment's badge only.
   - The source is a small seam, `app/map/alerts.py::open_alerts(db, deployment_ids)` returning
     `(entity_type, entity_id, deployment_id, count)`. **E7 adds Grafana alerts by extending
     that function**, not by touching the rollup.
   - **Authorized E3-owned edit:** `device_status.py` gains the alert input its own docstring
     names ("When they land, `alerting` outranks everything below it here, and this is the
     function that learns it"). It is applied through `hierarchy_common`'s readers, so the
     inventory tables and the map cannot disagree.
   - DECISIONS D191.

5. **The map has its own read endpoints, one Deployment at a time.**
   - `GET /map/deployments` is the Organization layer: one compact row per visible
     Deployment.
   - `GET /map/deployments/{id}` is one Deployment's Pods and Listeners.
   - `GET /map/devices/{entity_type}/{id}` is the drawer's status detail.
   - Spec 13 lists a resource surface, "not an exhaustive endpoint list". The inventory list
     endpoints paginate and would need many round trips per drill-down, and spec 14.4 says the
     map "never block[s] on a slow deployment". So the Organization layer never waits on a
     Deployment's detail. **Each endpoint's query count is constant in the number of devices**,
     and a test asserts it.
   - These are not E7's `/deployments/{id}/summary` or `/organization/summary`. Those carry
     telemetry and stay E7's.

6. **Locations.**
   - **Backend.** `backend/app/map/` holds `rollup.py` (pure), `alerts.py` (the seam),
     `service.py` (queries) and `basemaps.py` (settings to config). Routes live at
     `backend/app/api/map.py`.
   - **Frontend.** `src/lib/map.ts` (typed client, flat query keys under `["map", ...]`),
     `src/lib/basemaps.ts`, and `src/components/map/` for `MapView`, markers, `LeafFan`,
     `UnplacedPanel` and `DeviceDrawer`. Pure geometry and formatting live in `.ts` modules so
     they are unit-testable without Leaflet.
   - **Routes.** `/map`, `/map/deployments/:deploymentId` and `/map/pods/:podId`, with the open
     drawer in the query string (`?device=listener:02:EE:…`). Back, forward and deep links all
     work, and the Overview's "Open map" lands on the right Deployment.

7. **Libraries.** These npm packages are added, each pinned to an exact version recorded in
   DECISIONS: `leaflet` and `react-leaflet` (the React 18 line), and `leaflet.markercluster`,
   plus their `@types`. `pmtiles` and `protomaps-leaflet` are **dynamically imported**, only
   when a `pmtiles` source is configured, so online deploys do not ship them. There is no
   Google SDK. Leaflet's CSS is imported once, and its default PNG marker icons are never
   used: every marker is a CSS-drawn `DivIcon` built from tokens. **jsdom cannot lay out a
   map**, so marker behavior is asserted in vitest through the pure modules and component
   tests with a stubbed size. Real rendering is asserted in Playwright (E6.2b) against the
   `none` basemap. **No test makes a network request to a tile server.**

### Edits to earlier-phase code this phase is authorized to make

Each edit is additive and limited to what is listed here. Anything beyond this list is a
stop-and-ask.

| Owner | Edit | Task |
|---|---|---|
| E1 | `aggregator`/`deployment` coordinate columns; their In/Out/PATCH models (including the Pod-create body that creates the Aggregator); bulk import accepts optional `gps_lat`/`gps_lon` columns for aggregators; the inventory forms gain Latitude/Longitude | E6.2a |
| E1 | The demo fixture: Deployments and Aggregators get coordinates. Listener GPS becomes **relative to its Pod** (even-index Listeners keep GPS, so the fixture's leaf/pin mix is unchanged), and `inventory-fixture.ts` and the INTERFACES determinism rule move with it | E6.2a |
| SIM | `sim/provision.py` gives the fleet deterministic coordinates at all three levels, half of Listeners GPS-less | E6.2a |
| E3 | `device_status.py` gains the alert input (fixed choice 4); `hierarchy_common`'s readers pass it | E6.4 |
| E3 | `useLiveUpdates.ts` invalidates `["map"]` on `device_status` and `reconciliation` | E6.6 |
| E4 | `GET /provisioning/records?target_type=&target_id=`, a filtered read of existing rows (`VIEW_PROVISIONING`, D7 envelope), for the drawer's Card tab | E6.5 |
| E1 | `pages/Overview.tsx` becomes the V2·S1 Owner view (E6.7) | E6.7 |

**Untouched:** `contracts/mqtt.py`, `controlplane/consumer.py`, `services_status`'s writer, and
the four test-critical suites (RBAC, merge engine, revision state machine, provisioning
manifest).

## 3. Out of scope

Everything in this list belongs to another epic or spec item:

- **E7:** telemetry of any kind, including sparklines, Influx and Prometheus reads, "total
  audio collected", and any "cached Ns ago" figure. Also Grafana embeds and Grafana links (the
  drawer ships an honest placeholder that names E7, and no "Open in Grafana" button that goes
  nowhere), Grafana alerts, the webhook receiver and backfill, the `alerts` websocket channel,
  `GET /alerts` and the alert list, `GET /organization/summary`,
  `GET /deployments/{id}/summary`, and the Owner view's telemetry numbers.
- **E8:** security headers and CSP, including the tile-host allowlist that CSP will need (named
  in the handoff), the performance pass, and production licensing of tile sources (D188's
  remainder).
- **Spec 17 item 6:** live-GPS moving dots.
- **Not built at all:** geocoding and address search; tools that build PMTiles files (the
  operator supplies the file); setting coordinates by dragging a marker (coordinates are edited
  in the inventory forms, and DES.8 may file the drag interaction); and reusing the drawer from
  inventory rows, search results or alert rows (S2's note; E6 builds the component, and wiring
  it elsewhere is DES.8's).
- **Not E6's to change:** editing config from the drawer (it links to the E2.7 editor); any new
  status derivation beyond fixed choice 4; any writer of `services_status`; any new MQTT topic
  or message; and any edit outside section 2's authorized list.

## 4. Task list

The plan's E6.1-E6.7 are kept, with three changes recorded in project-changes #47:
- **E6.0** is added in front, as for SIM, E4 and E5.
- **E6.2 splits** into E6.2a (coordinates) and E6.2b (markers), following E5's lettered
  units.
- **E6.4 moves ahead of E6.2b**, because markers cannot be colored before the rollup exists.

**E6.8** is added for the walkthrough and SIM acceptance, following E4.11's precedent of a
named closing task. The working order is:

E6.0 → E6.1 → E6.2a → E6.4 → E6.2b → E6.3 → E6.5 → E6.6 → E6.7 → E6.8

**E6.0 Phase document and records.** This document and `project_planning/e6-progress-ledger.md`.
The records are:
- project-changes #47 (the task changes above and the batching), with addendum PLAN-3-05 on
  project plan §3 E6.
- project-changes #48 (the basemap default), with spec addendum SPEC-9-01.
- project-changes #49 (project-changes #9's map direction superseded), with addendum DES-7-04
  on the DES handoff.
- DECISIONS D188-D191.

Docs only, so by the owner's decision it gets **no gate of its own** (D192): it is committed
locally, `test_governance` is run on its own, and E6.1's full gate covers it before anything
is pushed or tagged. *Acceptance:* every fixed choice appears in
DECISIONS or is scheduled onto the task that implements it, the ledger has ten task rows, and
`test_governance` is green.

**E6.1 Map foundation.** Leaflet and react-leaflet in `MapView` inside `Map.tsx`'s existing
frame, with the `ContextBar` and `StatusLegend` kept.
- `lib/basemaps.ts` implements fixed choice 1's sources and factory. `app/map/basemaps.py`
  turns the `EOE_MAP_*` settings into `GET /map/config`. A basemap switcher control sits on
  the map, and attribution is always shown.
- Zoom controls and the map surface are restyled from tokens, with additive
  `tokens.ext.css` entries and dark values (D21), so the night theme is not a white map
  chrome on a dark page.
- The `EmptyState` placeholder is retired. The `none` basemap with no devices shows a short
  "No devices placed yet" note instead.

*Acceptance:*
- The config endpoint is RBAC-gated and offers exactly the sources whose settings are present.
- Offline mode removes every online source.
- An unset Google key means no Google source, asserted from the config, not the UI.
- `basemaps.ts` maps every source id to a layer factory, and a test fails if a new id is added
  without one.
- A Playwright test renders the map against `none` in light and dark themes and asserts the
  attribution and switcher exist.
- No test fetches a tile.
- The `EOE_MAP_*` names are added to `.env.example` and INTERFACES' environment table by name.

**E6.2a Coordinates for Aggregators and Deployments.** Fixed choice 2.
- The migration adds the columns and the both-or-neither CHECKs.
- The API models gain `gps_lat`/`gps_lon` on Aggregator and Deployment (out, create, PATCH),
  and on the Pod-create body's Aggregator.
- Aggregator bulk import accepts the two optional columns.
- The deployment form and the pod page's aggregator card gain Latitude/Longitude fields, using
  E4.11's half-coordinates message.
- Fixture and fleet coordinates move per section 2's table:
  - Redwood Coast is on the northern California coast and High Desert is in central Oregon.
  - Pods are about 1-2 km apart.
  - Listeners are within about 300 m of their Pod.
  - Everything is deterministic.

*Acceptance:*
- Migration up/down round-trips.
- A half-coordinate write is refused at the API with 422 and at the database by the CHECK.
- Out-of-range values are refused.
- Every edit audits through the E0.8 hook.
- Re-seeding the demo produces identical coordinates.
- No two demo Listeners in different Pods share a coordinate.
- Earlier walkthroughs that print fixture coordinates are amended in this batch (rule R1).

**E6.4 Status rollup engine and the map endpoints.** Fixed choices 3-5. The rollup is a pure
function in `app/map/rollup.py`. It takes descendant statuses, attached and unattached alert
counts and `services_status`, and returns the marker status. It also returns the counts per
status that labels and the Overview show. It **delegates ordering to `device_status.SEVERITY`**
and holds no order of its own.
- `GET /map/deployments` returns, per visible Deployment: id, name, slug, coordinates or null,
  status, counts by status (including `unknown`), alert count, `services_status`, pod count
  and listener count.
- `GET /map/deployments/{id}` returns:
  - **Pods:** pod id, name, aggregator id/uuid/name, the Aggregator's coordinates, the
    Aggregator's own status, the Pod's rolled-up status, counts, alert count and listener
    count.
  - **Listeners:** MAC, name, pod id, coordinates or null, status and alert count.

*Acceptance:*
- A table-driven rollup suite covers every severity pair, `unknown`-only, empty, each
  `services_status` value (only `degraded` moves the status), attached versus unattached
  alerts, and a sleeping-only Pod that rolls up `sleeping` and is not degraded.
- A test pins the frontend severity constant to `SEVERITY`.
- The inventory tables show `alerting` for a device with an open `duplicate_identity` alert.
  This is the authorized E3 edit, and the existing `device_status` tests stay green unchanged.
- Each endpoint's query count is constant from 1 to 1,000 Listeners, asserted with a
  statement counter.
- Scoping: a Deployment Operator assigned to one Deployment sees one row and gets a 404 for
  the other.
- Response time is under 200 ms at v1 scale (one Deployment, 20 Pods × 50 Listeners) on the
  gate host. This is recorded in the ledger as a measurement, not asserted as a flaky timing
  test.

**E6.2b Hierarchy markers and clustering.**
- **Deployment layer.** At `/map`, each placed Deployment gets a region marker: name, status
  chip (glyph and label), alert badge, and a services chip when not `verified`. The map fits
  bounds to all placed markers.
- **Pod layer.** At `/map/deployments/:id`, Pods are **square** markers sized by listener
  count, with status glyph and alert badge (S2/V2 legend). Listener **pins** with GPS are
  shown, clustered with `leaflet.markercluster`. A cluster's icon shows its count and the
  worst status among its children, computed with the pinned severity constant.
- **Pod focus.** `/map/pods/:podId` zooms to the Pod and its Listeners.
- **Navigation.** `ContextBar` crumbs (Organization / Deployment / Pod) are real links.
- **Tabs.** *Status* colors by status. *Reconciliation* dims every device whose latest
  revision is `applied`, so drifted, failed and pending stand out. *Alerts* dims every device
  without an alert badge, and until E7 says so in a single line.
- **Unplaced panel.** It lists the Deployments and Pods without coordinates at the current
  level.
- **Accessibility.** Markers are keyboard-focusable buttons whose accessible name carries
  level, name, status and alert count. The status always uses all three channels. `unknown`
  renders as a neutral marker with a dash, never a colored one.

*Acceptance:*
- A Playwright test against a mocked API and `none` basemap shows: two Deployment markers with
  correct glyph and label; drill-down by click and by crumb; Pod squares; pins; a cluster whose
  icon carries the worst child status; and Tab focus reaching markers.
- vitest covers marker accessible names, the `unknown` marker, the services chip rule, and tab
  dimming.
- Every new class lands in an `app.css` section, and every new token is additive with a dark
  value, with `tokens.test.ts` green.
- No `[data-status]` element renders for an `unknown` device (the D60 guard extended to the
  map).

**E6.3 Leaf versus pin logic.** Spec 9.2.
- **Leaves.** A GPS-less Listener is a **leaf**, fanned out around its Pod's marker on a short
  dashed tether. The fan is a pure function in `components/map/leafFan.ts`: n leaves to
  screen-space offsets on one or more rings, with a fixed pixel radius, so leaves never claim a
  geographic position and do not drift when zooming. Leaves are drawn hollow or with a dashed
  outline, visibly unlike pins.
- **Pod badge.** The Pod marker carries an "N no GPS" badge.
- **Toggle.** A *Show GPS-less* control (on by default, remembered per browser) hides and
  shows leaves.
- **Pin placement.** When a Listener gains coordinates (E4.11's fill-in or the inventory
  form), it becomes a pin at its coordinates. It loses its leaf, and the Pod's badge count
  drops.
- **Unplaced Pods.** A GPS-less Listener whose Pod is unplaced has nothing to orbit, so it is
  listed under its Pod in the Unplaced panel.

*Acceptance:*
- The fan function is unit-tested for 1, 8, 50 and 51 leaves: no two leaves overlap at the
  marker size, rings grow outward, and the output is deterministic.
- A component test gives a leaf Listener coordinates through the PATCH mutation in the same
  tab, and the leaf becomes a pin without a reload.
- Leaves and pins are distinguishable without color (outline style), asserted on classes.
- Playwright shows the fan around a demo Pod.

**E6.5 Device detail drawer.** `components/map/DeviceDrawer.tsx`, following the V2 drawer
treatment in full:
- The map dims behind the drawer and the selected marker keeps a halo.
- A leader line runs from the marker to the drawer edge while the marker is in view, and is
  hidden otherwise.
- The drawer has a deep shadow and a 3px accent edge, and its header carries a title, a Close
  button and an Esc hint.
- Focus moves into the drawer on open and returns to the marker on close.
- The drawer's state is in the URL.

The tabs:
- **Status.**
  - *Listener:* spec 6.5 liveness (state, last audio, expected wake, grace and where the grace
    is inherited from), placement, and open alerts with their detail.
  - *Aggregator:* LWT online/offline and since when, and the latest revision state.
  - *Pod:* its Aggregator's status plus counts.
  - *Deployment:* counts and per-service chips from E5.5.
  - The backend for this tab is `GET /map/devices/{entity_type}/{id}`.
- **Config.** The effective config with provenance, read-only, from E2.4, and an "Edit at this
  level" link to the E2.7 editor (`manage_config`).
- **Timeline.** `DeviceTimeline`.
- **Card.** The latest provisioning record via the new filtered read, shown with `RecordChip`
  (`view_provisioning`).
- **Telemetry.** An `EmptyState` naming E7, with no fake chart.

Actions: **Resync** for an Aggregator (E3.10 command, gated as the endpoint is) and **Open in
inventory**.

*Acceptance:*
- Each tab renders real fixture data in vitest.
- A Viewer sees no Resync and no Edit link. A Field Tech sees the Card tab but not the Config
  tab's edit link.
- Esc closes the drawer, focus returns to the marker, and the halo follows the selection.
- A deep link to `?device=listener:…` opens the drawer on load.
- A secret config key shows the redacted marker and never a value, asserted with the E2
  secret fixture.

**E6.6 Live map updates.** `useLiveUpdates` invalidates `["map"]` on `device_status` and
`reconciliation`.
- **Coalescing.** Map refetches are coalesced to at most one per key every 2 s. Events stay
  invalidation signals, and a coalesced refetch still fetches the latest data, so nothing is
  missed.
- **Mutations.** Inventory mutations in the same tab (coordinates, names) invalidate
  `["map"]`.
- **Polling.** A 30 s refetch interval covers the changes that emit no event today
  (`services_status` and `inventory_alert`). This is documented as interim, and E7's `alerts`
  channel replaces the alert half.
- **Marker identity.** Markers are keyed by entity id, so a refetch updates them **in place**:
  zoom, pan, cluster expansion, the open drawer and its tab all survive a live update.

*Acceptance:*
- A vitest test drives a fake socket: a `device_status` event leads to one refetch, the marker
  changes, and the map view and open drawer are unchanged.
- A burst of 200 events produces at most one refetch per window.
- A backend test asserts that the `disconnect` scenario's LWT event carries the
  `deployment_id` the map's subscription filter needs.
- The end-to-end proof (SIM `disconnect` turns a marker offline with no reload) is E6.8's.

**E6.7 Owner landing view.** `Overview.tsx` becomes V2·S1:
- The hero number is **Devices online / registered**. Online means any status other than
  `offline` and `unknown`; registered means every Aggregator and Listener in scope.
- One card per Deployment shows its rollup chip, status distribution chips, per-service chips,
  its Pods with status, and **Open map** (to `/map/deployments/:id`).
- A **Needs attention** panel lists open inventory alerts, then offline, drifted and degraded
  devices, worst first and capped with a "show all" link to the map's Alerts or Reconciliation
  tab.
- Telemetry figures ("TB collected", cache age) are an honest line naming E7.
- The data comes from E6.4's endpoints only.

*Acceptance:*
- vitest shows the hero, cards and attention list against the fixture.
- A Viewer and a Field Tech see the page with no manage actions.
- An empty organization still shows E1's empty state.
- The E1/E3 Overview tests are amended deliberately, not deleted, and the amendments are noted
  in DECISIONS.

**E6.8 Verification walkthrough and SIM acceptance.**
- **Walkthrough.** `guide/e6-verification.md`, indexed in `guide/README.md` (rule R1, enforced
  by `test_governance`), covering every task by hand against a clean QA stack. It amends any
  assertion in `e1`-`e5`/`sim` walkthroughs that this epic invalidates, in this batch.
  Examples are the Map "no imagery yet" line, the Overview's "status arrives with E3" lines,
  and fixture coordinates.
- **SIM test.** A SIM-stage test (`sim/tests/test_map_fleet.py`) provisions a small fleet with
  coordinates, waits for it to report, asserts `/map/deployments/{id}` places every Pod and
  rolls up healthy, then runs `disconnect` on one Aggregator and asserts that Pod and its
  Deployment roll up `offline`. Then it runs `missed_wake_window` and asserts the Listener is
  `offline` while its Pod's Aggregator stays online.

*Acceptance:*
- The walkthrough runs start to finish by hand on a clean stack, and the defects it finds are
  fixed and recorded.
- The SIM test is green inside the gate's SIM stage, within the stage's existing time budget
  or with the budget raised deliberately and recorded.

## 5. Definition of done

An operator opens Map and drills from Organization to a single Listener on satellite imagery.
Markers are rolled up worst-of-descendants per spec 9.3, including services `degraded` and open
alerts. GPS-less Listeners fan out as leaves until they get coordinates, then become pins. The
detail drawer shows real status, config, timeline and card. A device going offline in a SIM
fleet changes its marker within seconds without a reload. The Overview shows online/registered
and a card per Deployment. In addition:

- **One derivation.** The map, the inventory tables and the Overview cannot disagree about a
  device's status, because all three read `device_status.py` through the same readers.
- **Honest status.** `unknown` is never drawn as a colored marker. Nothing shows telemetry,
  Grafana data or alerts that do not exist yet. Each placeholder names the epic that fills it.
- **No network in tests.** No test reaches a tile server. The `none` basemap carries every
  automated rendering check.
- **Basemaps.** Every source is behind the `BasemapSource` interface. The Google source
  appears only with a key, and offline mode shows no online source.
- **The universal definition of done** (handbook section 4): CI is green, every mutation is
  audited, every new endpoint is RBAC-gated, `INTERFACES.md` and `DECISIONS.md` are updated,
  the demo fixture still seeds, and the compose stack still starts clean.

## 6. Handoff artifacts

- **`docs/INTERFACES.md` gains an "Owned by E6" section** containing:
  - the `/map/*` endpoints and their shapes;
  - `app/map/rollup.py` and the services rule;
  - the `open_alerts` seam, marked **E7 EXTENDS THIS**;
  - the `BasemapSource` interface and the `EOE_MAP_*` settings;
  - the map routes and the `?device=` deep-link grammar (E7's alert rows link into it);
  - the `["map"]` query keys and the coalescing rule;
  - the coordinate columns' both-or-neither contract;
  - a note for **E8**: the tile hosts a CSP `img-src`/`connect-src` must allow per configured
    source (`server.arcgisonline.com`, `tile.openstreetmap.org`, `tile.googleapis.com`, and the
    PMTiles origin).
- **Additive rows in other sections:**
  - E1's entity schema and fixture sections record the new columns and the moved determinism
    rule.
  - E3's status section records the alert input.
  - E4's records section records the filtered read.
  - SIM's provisioning section records fleet coordinates.
- **`docs/DECISIONS.md`:** D188-D191 with their open remainders (Esri production terms, the
  30 s interim poll), plus every deviation found while building.
- **`guide/e6-verification.md`**, and the amendments this epic forces on earlier walkthroughs.
- **`project_planning/e6-progress-ledger.md`**, kept current per task.
- **`deploy/.env.example`:** `EOE_MAP_OFFLINE`, `EOE_MAP_GOOGLE_API_KEY`,
  `EOE_MAP_PMTILES_URL`, `EOE_MAP_IMAGE_URL` and `EOE_MAP_IMAGE_BOUNDS`, by name only. The
  Google key's referrer restriction is stated beside it.

## 7. Batches and owner checkpoints

There is one PR per batch, on `e6-batch-N`. **Each batch is branched from `main` only after
the previous batch's PR has merged** (E4's convention, project-changes #43). Every task still
ends on a full gate (rule R0).

**Every E6 batch changes the UI, so none is merged without the owner's hands-on check.** At
the end of each batch, the QA stack is brought up and the owner is given the checklist below
with the exact URL and login. Each checklist is chosen to catch, while it is still cheap to
fix, the defects that later tasks would otherwise build on.

| Batch | Tasks | Checkpoint |
|---|---|---|
| **B1** | E6.0, E6.1, E6.2a | CP1: imagery and coordinates |
| **B2** | E6.4, E6.2b, E6.3 | CP2: markers, rollup, leaves |
| **B3** | E6.5, E6.6 | CP3: drawer and live updates |
| **B4** | E6.7, E6.8 | CP4: Owner view and full walkthrough |

**CP1: imagery and coordinates.** Every later task draws on top of the basemap and reads these
columns.
- **Map.** Open *Map*.
  - Satellite imagery fills the region, and the legend is still there.
  - The basemap switcher lists **Satellite (Esri)** and **Street (OSM)**, and **no Google
    entry**.
  - Attribution is visible in a corner for each basemap.
  - Switch to Street, reload, and it is still Street.
  - Zoom and pan are smooth.
  - The zoom controls and switcher are readable in **both themes**.
- **Coordinates.** Open *Inventory* → a Deployment's edit form, and a Pod's aggregator card.
  - Both have Latitude/Longitude.
  - Entering only one of the two is refused, with a message saying both are needed.
  - Latitude 95 is refused.
  - Saving a valid pair sticks after reload.
  - Demo Deployments and Aggregators already have coordinates.
- **What to report:** any blank or grey map, any missing attribution, any field that accepts a
  half pair.

**CP2: markers, rollup and leaves.** The drawer and live updates attach to these markers, so
their status and placement must be right first.
- **Organization view.**
  - Two Deployment markers sit roughly over northern California and central Oregon.
  - Each shows a glyph, a label and a color.
  - A Deployment whose services are not verified shows a **services** chip, but its color does
    not turn degraded because of it.
- **Drill-down.**
  - Click Redwood Coast. The map zooms to three **square** Pod markers sized by listener
    count, and the crumbs read Organization / Redwood Coast.
  - Click a Pod. Pins sit where Listeners have GPS, and the GPS-less ones fan out around the
    Pod on dashed tethers. The fanned leaves look different from pins **even in greyscale**,
    and the Pod shows "N no GPS".
  - The *Show GPS-less* toggle hides the fan.
  - Zoom out and pins merge into clusters showing the worst status inside.
- **Unplaced.** Clear a Pod's aggregator coordinates in Inventory. The Pod disappears from the
  map and appears in *Unplaced* with a link back to the form.
- **Status rollup.** With a SIM device stopped or a scenario run (the exact commands go in the
  checklist), its Pod and Deployment turn **Offline**, while Listeners that were never heard
  from show a dash, never a color.
- **Tabs.** *Reconciliation* and *Alerts* dim everything that is fine.
- **Keyboard.** Tab reaches the markers.
- **What to report:** any marker with no glyph or label, overlapping leaves, a status that
  disagrees with the Inventory table for the same device, and anything slow at Pod zoom.

**CP3: drawer and live updates.** E7 adds telemetry and alerts into this drawer.
- **Drawer.**
  - Click a Listener. The map dims, the marker keeps a halo, and a line connects it to the
    drawer.
  - Esc closes the drawer and focus returns to the marker.
  - *Status* shows liveness times.
  - *Config* shows effective values with where each came from, and secrets are masked.
  - *Timeline* shows the device's history.
  - *Card* shows its provisioning status.
  - *Telemetry* says it arrives with E7.
  - Copy the URL into a new tab and the same drawer opens.
- **Roles.** As a Viewer, there is no Resync and no Edit link.
- **Live updates.**
  - With the drawer open, run the `disconnect` scenario. Within a few seconds, with **no
    reload**, the marker and the drawer both go Offline. The zoom and the open tab do not
    move.
  - In a second tab, give a GPS-less Listener coordinates. In the first tab, its leaf becomes
    a pin within about 30 s.
- **What to report:** the drawer losing its place on an update, any jump of the map view, and
  stale status after reconnecting the network.

**CP4: Owner view and full walkthrough.**
- **Overview.**
  - The hero reads *Devices online N / M*.
  - Each Deployment card shows its rollup, distribution, service chips and Pods.
  - *Open map* lands on that Deployment.
  - *Needs attention* lists the device you disconnected.
- **Roles.** Sign in as each role and confirm what each one sees.
- **Walkthrough.** Then run `guide/e6-verification.md` start to finish.
- **What to report:** any number on the Overview that disagrees with the map.
