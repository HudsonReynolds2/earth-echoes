# E6 Verification Walkthrough

A hands-on test platform for everything Epic E6 ships: the map and monitoring. That covers
satellite basemaps, coordinates, status rollup, markers and clusters, GPS-less leaves, the
device drawer, live updates, the Unreachable status and the Owner view. Work top to bottom,
ticking each box. Every step says what to do and what you should see. Anything else is a
finding worth writing down.

This is a living acceptance document (rule R1): E6 ships it, and any later epic that
invalidates an assertion here amends it in the same batch. Siblings: [E1](e1-verification.md)
(hierarchy and inventory), [E2](e2-verification.md) (configuration),
[E3](e3-verification.md) (control plane), [E4](e4-verification.md) (provisioning),
[E5](e5-verification.md) (services) and [SIM](sim-verification.md) (the simulation harness).

**E6 amends two siblings, in this same batch:**

- **E1 §1 and the not-yet-filled surfaces list.** The Overview now shows real status, and the
  legend has seven states.
- **E3's status section.** A Listener behind an offline Aggregator now reads Unreachable.

**The property that matters most in this epic: the map, the inventory tables, the drawer and
the Overview are one derivation.** A device's status is computed once, on the server (D60),
so the same device can never read differently on two screens. Several steps below check that
directly. A disagreement anywhere is a defect, never a rounding difference.

## 0. Start the platform and a fleet

The QA stack on a POSIX host (on Windows, `.\qa-stack.ps1` does the same). From the repo
root:

```bash
set -a; . deploy/.env; set +a
(cd backend && uv run python -m app.devbroker --certs-only)
docker compose -f deploy/docker-compose.yml -p eoe-qa --env-file deploy/.env up -d --build --wait
export DATABASE_URL="${DATABASE_URL/@postgres:5432/@localhost:15432}"
(cd backend && uv run alembic upgrade head)      # the stack does NOT migrate on start
(cd backend && uv run python -m app.seed --demo) # prints the owner password ONCE
(cd backend && uv run python -m app.devbroker --host mosquitto --keep-tls)
docker compose -f deploy/docker-compose.yml -p eoe-qa --env-file deploy/.env restart mosquitto
```

- [ ] If the seed fails to connect, an older stack under the same project name may hold a
      database volume created with different credentials. Use another project name in every
      command (`-p eoe-e6` is the one used to build this walkthrough), or `down -v` that
      project if its data does not matter.
- [ ] `http://localhost:15173` signs in as `owner@example.com`, with the password the seed
      printed.
- [ ] `deploy/.env` has `EOE_CORS_ORIGINS=http://localhost:15173`. If the browser login
      fails with "NetworkError", that is the line to check.
- [ ] **Take the QA stack down before you ever run the test gate** (`docker compose ... down`,
      volumes kept). The gate's containers bind the same host ports (D44).

A small simulated fleet makes the statuses real. In a second terminal (`read` keeps the
password off the screen and out of the history):

```bash
set -a; . deploy/.env; set +a
export DATABASE_URL="${DATABASE_URL/@postgres:5432/@localhost:15432}"
export EOE_SIM_OPERATOR=owner@example.com
read -rs EOE_SIM_PASSWORD; export EOE_SIM_PASSWORD
cd sim
uv run python provision.py --aggregators 2 --listeners-per-aggregator 3 --platform-broker mosquitto:8883
cd .. && docker compose -f deploy/docker-compose.yml -p eoe-qa --env-file deploy/.env restart mosquitto api worker
```

- [ ] Wait for the API to answer before starting any fleet:
      `until curl -sf -o /dev/null http://localhost:18000/api/v1/health; do sleep 2; done`. A
      fleet started during the restart fails with "connection reset by peer".
- [ ] If a fleet says "no broker account for sim-fleet-sim-000", the broker's account files were
      regenerated from another database (a test gate does this). Re-run
      `(cd backend && uv run python -m app.devbroker --host mosquitto --keep-tls)`, then restart
      `mosquitto api worker` as above.
- [ ] It ends `provisioned 2 × 3 (6 Listeners) in sim-fleet`. The `sim-fleet` Deployment, its
      two Pods and half its Listeners have coordinates (E6.2a), and the rest are GPS-less.

## 1. The basemap (E6.1)

- [ ] Open **Map**. Satellite imagery (Esri) fills the region, the legend sits bottom left,
      and attribution is in a corner.
- [ ] The **Basemap** switcher lists *Satellite (Esri)* and *Street (OSM)*, and no Google
      entry unless `EOE_MAP_GOOGLE_API_KEY` is set. Switch to Street, reload: still Street.
- [ ] The zoom controls and switcher are readable in **both themes** (Night toggle).
- [ ] With `EOE_MAP_OFFLINE=true` in `deploy/.env` and the API restarted, the online sources
      disappear from the switcher. Put it back afterwards.

## 2. Coordinates, and the both-or-neither rule (E6.2a, H1)

- [ ] **Inventory → Redwood Coast** has a *Map location* card with Latitude/Longitude, and so
      does each Pod's aggregator card. The demo Deployments and Aggregators already have
      coordinates.
- [ ] Enter only a latitude and save: refused with "give both latitude and longitude, or
      neither". Latitude `95`: refused. A valid pair: saved, and it survives a reload.
- [ ] **H1 (D199): Listeners follow the same rule.** On a Listener's page, *Edit* with only a
      latitude is refused with the same message. Through the API, a half pair is a 422 for
      create and PATCH alike, and a bulk-import row with half a pair is a `validation_error`
      row. A PATCH that only renames a Listener does not judge its coordinates.

## 3. Markers, drill-down and clusters (E6.4, E6.2b)

- [ ] **Organization view:** markers for Redwood Coast (northern California), High Desert
      (central Oregon) and SIM Fleet. Each has a glyph, a label and a color. A Deployment
      whose services are not verified shows a **services** chip, and its color does not turn
      degraded because of it (D190).
- [ ] **Unplaced:** a Deployment with no coordinates is listed under *Unplaced* with a link to
      its form, not drawn somewhere invented.
- [ ] Click **Redwood Coast**. The map zooms to three **square** Pods sized by Listener count,
      and the crumbs read Organization / Redwood Coast. Click a Pod: its route is
      `/map/pods/<id>`, and Back returns.
- [ ] Zoom out on a Pod: its pins merge into a cluster, which shows the **worst** status
      inside it.
- [ ] Clear a Pod's aggregator coordinates in Inventory. The Pod leaves the map and appears in
      *Unplaced*. Put them back.
- [ ] **Never-heard devices are honest:** on a fresh seed, demo markers show a dash and "No
      status yet", never a color.
- [ ] **Lenses:** *Reconciliation* keeps drifted and degraded devices bright and dims the rest.
      *Alerts* keeps devices with open alerts bright.
- [ ] **Keyboard:** Tab reaches the markers, and Enter drills or opens.

## 4. GPS-less Listeners as leaves (E6.3)

- [ ] On a Pod's route, its GPS-less Listeners fan out over the Pod as hollow, dashed leaves on
      tethers. They read differently from pins even in greyscale (try your OS's greyscale
      filter). The Pod's label says "N no GPS".
- [ ] *Show GPS-less* hides the fan, and the choice survives a reload.
- [ ] Give a GPS-less Listener coordinates in Inventory. Back on the map, it is now a pin.

## 5. The device drawer (E6.5)

- [ ] Click a Listener (pin or leaf). The map dims, the marker keeps a halo, and a line runs
      from the marker to the drawer. The drawer has a titled header, an **Esc** hint and
      **Close**, and focus lands on its title.
- [ ] **Status:** liveness times (state, last audio, expected wake, last report), the grace
      with where it is inherited from, placement, and any open alerts with their detail.
- [ ] **Config:** effective values, each with the level it resolves from. A secret shows as
      bullets, never a value. *Edit at this level* opens the editor for that device, and is
      only shown to a role that may edit config.
- [ ] **Timeline:** the device's revision history. **Card:** its latest provisioning record,
      or "No card yet". **Telemetry:** says it arrives with E7, with no fake chart.
- [ ] Copy the URL (it carries `?device=` and `?panel=`) into a new tab: the same drawer opens
      on the same tab.
- [ ] Press **Esc**: the drawer closes, the URL drops `device=`, and focus returns to the
      marker.
- [ ] On a Pod's route, **Pod details** opens the Pod's drawer: its Aggregator, counts and
      placement. Its Aggregator's name opens the Aggregator's drawer, which offers **Resync**
      to an owner or operator.
- [ ] **Roles:** create a Viewer (Users page) and sign in as them. The drawer shows no Resync
      and no Edit link. A Field Tech sees the Card tab, but no Edit link.

## 6. Live updates, and Unreachable (E6.6, E6.9)

Open **Map → SIM Fleet → SIM Pod 000 → Pod details**, then start the fleet with a disconnect
in the second terminal:

```bash
cd sim && uv run python fleet.py --aggregators 2 --listeners-per-aggregator 3 \
  --scenario disconnect --scenario-devices 1 --stay
```

- [ ] The fleet connects, applies a revision, and kills `sim-fleet-sim-000` without a
      DISCONNECT (its log says so).
- [ ] **With no reload**, within a few seconds the Pod marker and the open drawer both turn
      **Offline**. The zoom, the open drawer and its tab do not move.
- [ ] SIM Pod 000's Listeners read **Unreachable**: a hollow hexagon in a darkened yellow
      (D202). Open one: a note names the offline aggregator and since when, followed by
      "Last reported liveness", which is history, not the present.
- [ ] **One derivation:** the same Listener reads Unreachable on its Inventory page too. SIM
      Pod 001 and its Listeners read Streaming.
- [ ] Ctrl-C the fleet: it says goodbye politely, so both Aggregators go Offline within
      seconds, and every SIM Listener reads Unreachable.
- [ ] **Coalescing:** in the browser's devtools Network tab, a burst of events produces at
      most one map refetch every 2 seconds, not one per event (D201).
- [ ] **No event, still fresh:** in a second browser tab, give a GPS-less Listener coordinates.
      The first tab's leaf becomes a pin within about 30 s (the interim poll).

## 7. The Owner view (E6.7)

- [ ] **Overview** reads **Devices online N / M**. M counts every Aggregator and Listener in
      scope. N excludes offline, unreachable and never-heard devices. With the fleet stopped
      after §6, the SIM devices are not online.
- [ ] Re-run the §6 fleet command and wait for the disconnect. The hero becomes **4 / 42**
      (SIM Pod 001's Aggregator and its 3 Listeners, out of the 8 Aggregators and 34
      Listeners in the seeded demo plus the SIM fleet).
- [ ] Each Deployment has a card: its rollup chip, distribution chips (SIM Fleet: 1 offline ·
      3 unreachable · 4 streaming), one chip per service, its Pods with status, and **Open
      map**, which lands on that Deployment.
- [ ] **Needs attention** lists `sim-fleet-sim-000` (Offline) and **one** row "3 listeners
      unreachable" for SIM Pod 000, not three. Each row opens the right drawer or Pod.
      With more than 8 rows, "Show all on the map" links to the Alerts and Reconciliation
      lenses (`/map?tab=…`).
- [ ] Telemetry figures (recordings collected) say they arrive with E7.
- [ ] Sign in as the Viewer from §5: the page is the same, with no *Open inventory* in the
      header.

## 8. The SIM acceptance test

`sim/tests/test_map_fleet.py` runs in the gate's SIM stage. It connects the demo Redwood
Coast fleet as mock devices, then asserts the map read is healthy. It runs `disconnect`
(the Pod and Deployment go offline, and its Listeners go unreachable), then
`missed_wake_window` (one Listener goes offline while its Aggregator stays online). It reads
only `GET /map/deployments/{id}`, as the browser does.

- [ ] `make gate` passes with it, the whole suite, no filters.
