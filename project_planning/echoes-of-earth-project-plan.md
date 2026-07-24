# Echoes of Earth Management Platform: Project Development Plan

**Version:** 1.0
**Date:** 2026-07-11
**Companion document:** Technical Specification v1.1
**Purpose:** Break the spec into Jira-ready epics, tasks, and dependency ordering, and define the phase documents that hand each area of work to an implementation session.

---

## 1. How to use this document

Each epic below becomes a Jira epic. Each numbered task becomes a story or task under it. Task IDs (for example E3.4) exist only so dependency notes can point at specific tasks; drop them or keep them as labels in Jira as you prefer. The "Depends on" line under each epic sets the ordering between epics, and Section 4 draws the full graph. Section 5 defines the phase documents: one per implementation epic, handed to a fresh conversation together with spec v1.1.

Two tracks run in parallel. The design track (DES) produces decisions and Figma assets, not code, and never blocks implementation because the frontend builds against neutral design tokens (spec 3.2). The implementation track (E0 through E8, plus SIM) follows the spec's phase breakdown (spec 18).

---

## 2. Epic summary

| Epic | Name | Spec sections | Depends on |
|------|------|---------------|------------|
| DES | Design track (Figma and UX) | 3.2 | none (application tasks need E2) |
| E0 | Foundations | 12, 15 | none |
| E1 | Hierarchy and inventory | 4, 13 | E0 |
| E2 | Configuration model | 5 | E1 |
| E3 | Control plane and reconciliation | 6, 7 | E0, E1 (E2 for full loop) |
| SIM | Simulation harness | 7, 14.2 | E0 (contracts from spec 7; converges with E3) |
| E4 | Provisioning tool | 8, 16.4 | E1, E2 (E5.6 for bootstrap block) |
| E5 | Deployment services onboarding | 16 | E0, E1, E3 |
| E6 | Map and monitoring | 9 | E1, E3 (E5 for services status) |
| E7 | Telemetry and alerts | 10, 11 | E1, E5, E6 |
| E8 | Hardening and cloud | 14, 15 | all implementation epics |

---

## 3. Epics and tasks

### DES: Design track (Figma and UX)

Goal: reach agreement on the visual design system and UX patterns, produce the Figma assets, and later apply the chosen system to the implemented UI. This track runs on its own schedule. Implementation never waits on it because all theming sits behind design tokens.

Definition of done: a decided CSS profile expressed as a token sheet, a Figma component library covering the core screens, UX-reviewed flows for the four wizards and the map, and the design system applied to the live frontend.

1. **DES.1 Screen and flow inventory.** List every screen and flow the platform needs (map, inventory tables, config editor, bulk edit preview, provisioning wizard, services onboarding wizard, device detail panel, timeline, alerts list, auth and admin). Output: a shared checklist that scopes the rest of the track.
2. **DES.2 CSS profile candidates.** Assemble two to three candidate visual directions (color, typography, density, component styling) as Figma boards for group review.
3. **DES.3 CSS profile decision.** Review session; pick one direction. Record the decision and rationale.
4. **DES.4 Design token sheet.** Translate the chosen profile into a concrete token sheet (CSS variable names and values for color, spacing, type scale, radii, elevation) matching the token structure the frontend skeleton defines in E0.4.
5. **DES.5 Component library in Figma.** Build the reusable components (buttons, forms, tables, cards, status badges, map markers, wizard steps) in the chosen style.
6. **DES.6 UX pass on key flows.** Wireframe and usability-review the provisioning wizard, the services onboarding wizard (both paths), the bulk config edit preview, and the map drill-down. Output: annotated flows the implementation epics follow. (Feeds E4.10, E5.12, E2.8, E6.)
7. **DES.7 Apply the design system.** Load the DES.4 token values into the frontend, restyle components against DES.5, and resolve deviations. Depends on DES.4, DES.5, and at least E1/E2 frontend surface existing. Schedule anywhere from mid-E2 onward; before E6 is ideal so the map builds styled.
8. **DES.8 Usability review of the implemented UI.** Walk the real flows once E4 through E6 land, file UX fixes as tasks. Ongoing.

### E0: Foundations

Goal: a running skeleton with auth, so every later epic adds features to a live system.

Definition of done: `docker compose up` yields an API and frontend with login, roles enforced on a sample endpoint, migrations, CI green, and audit rows written for mutations.

1. **E0.1 Repository and container scaffolding.** Monorepo layout, Dockerfiles, Compose stack (`api`, `frontend`, `postgres`, optional `redis`), dev environment docs.
2. **E0.2 Postgres and migrations.** Alembic wiring, base migration, migration conventions.
3. **E0.3 FastAPI skeleton.** App factory, settings from env plus config file (spec 15.3), health endpoint, versioned `/api/v1` prefix, Pydantic boundary conventions, error envelope.
4. **E0.4 React skeleton with neutral design tokens.** Vite app, routing, TanStack Query setup, the CSS variable token structure (spec 3.2) populated with neutral defaults, base layout shell.
5. **E0.5 CI pipeline.** Lint, typecheck, tests, container build on every push.
6. **E0.6 Local accounts and sessions.** Argon2id hashing, signed expiring session tokens, login/logout endpoints and UI.
7. **E0.7 RBAC framework.** The four roles (spec 12.3), deployment-scoped assignment model, permission check dependency applied at the API layer, UI role gating helper.
8. **E0.8 Audit log.** Immutable audit table, write hook on every mutation, `GET /audit` with filters.
9. **E0.9 User administration.** User CRUD, role and scope assignment endpoints and UI.
10. **E0.10 Optional TOTP.** Enrollment and verification for privileged roles.
11. **E0.11 Platform secrets envelope encryption.** The platform-side KEK/DEK storage layer (spec 12.4) behind an interface (env KEK now, secret manager later). E5 and E4 both consume this, so it lands here.

> **Addendum PLAN-3-01 (2026-07-23, ref project-changes #2):** E0 carries a twelfth task not listed above: E0.12 Seed script (dev seed creating the initial owner account; fresh environment to logged-in owner in one command), defined in phase-0-foundations.md section 4. Add it as a story when creating the Jira board.

### E1: Hierarchy and inventory

Goal: the Organization to Listener data model with full CRUD, validation, and import.

Definition of done: an operator creates the full hierarchy in the UI, bulk-imports listeners from CSV with validation results, duplicate rules enforced per spec 4.3, tags editable.

1. **E1.1 Hierarchy schema.** Tables for Organization, Deployment, Pod, Aggregator, Listener with the identity rules of spec 4.2 (`aggregator_uuid` first-class and indexed, Listener keyed by MAC).
2. **E1.2 CRUD endpoints.** The spec 13 hierarchy surface, pagination, filtering, sorting.
3. **E1.3 One Aggregator per Pod enforcement.** Create-or-attach on Pod creation, reject second attach.
4. **E1.4 Uniqueness validation.** Listener name unique per Deployment with auto-suffix option, MAC unique globally with hard reject (spec 4.3 item 1).
5. **E1.5 Duplicate identity handling at report time.** `duplicate_identity` quarantine and `provisioning_required` membership check (spec 4.3 items 2 and 3). Ships as model plus service logic now; E3 wires it to live reported messages.
6. **E1.6 Bulk import.** CSV/JSON import for Listeners and Aggregators with per-row validation results.
7. **E1.7 Tags.** Free-form tags on every entity, `GET/PUT /{entity}/{id}/tags`.
8. **E1.8 Inventory UI.** Hierarchy tree navigation and device tables (TanStack Table) with create/edit forms.

### E2: Configuration model

Goal: the settings catalog, sparse overrides, merge engine, and selection machinery.

Definition of done: setting a value at any level changes the computed effective config of descendants, preview shows affected devices before commit, tag and query selection works, all covered by merge-engine tests.

1. **E2.1 Versioned settings catalog.** The spec 5.3 catalog stored as versioned data, including the deployment service settings rows, with type, enum, range, secret flag, lowest level.
2. **E2.2 Sparse override storage.** JSONB override map per entity, validated against the catalog.
3. **E2.3 Effective-config merge engine.** The spec 5.1 deep merge with full unit test coverage (this is one of the four test-critical components in spec 14.5).
4. **E2.4 Effective and override endpoints.** `GET .../config/effective`, `GET/PUT .../config/overrides`.
5. **E2.5 Selection engine.** Tag and attribute query language, `POST /selections/preview`, saved selections.
6. **E2.6 Bulk preview and apply endpoints.** `POST /config/preview` and `POST /config/apply` (apply stubs revision creation until E3 publishes it).
7. **E2.7 Schema-driven config editor UI.** Editors rendered from the catalog (type, enum, range, secret masking), per-level override view showing inheritance.
8. **E2.8 Bulk edit UI.** Selection builder, affected-device preview table, commit flow. (Follows DES.6 wireframes if available.)

### E3: Control plane and reconciliation

Goal: live MQTT desired/reported flow with the revision state machine.

Definition of done: publishing a config change drives a (mock) Aggregator through pending to applied, drift and timeout transitions fire per spec 6.2, the per-device timeline renders history, LWT flips status.

1. **E3.1 Broker for development.** Mosquitto in the dev Compose stack with TLS and a platform account, matching spec 7.1.
2. **E3.2 MQTT client manager.** Async connection per deployment broker, reconnect handling, credential loading from E0.11 storage.
3. **E3.3 Topic contracts and schemas.** The spec 7.2 namespace and 7.3 payload models as typed Pydantic schemas with `schema_version`, shared as the contract SIM builds against.
4. **E3.4 Desired publish path.** Retained publish of `config_revision` to Aggregator and Listener desired topics.
5. **E3.5 Reported consumer.** Subscribe to reported and event topics, idempotent handling by revision id plus checksum, out-of-order tolerance (spec 7.4), wiring into the E1.5 duplicate/quarantine logic.
6. **E3.6 Revision state machine.** All spec 6.2 states and transitions with full unit test coverage (test-critical per spec 14.5).
7. **E3.7 Reconciliation worker.** The spec 6.4 loop: recompute on change, publish, compare, timeout, periodic drift re-check.
8. **E3.8 LWT status handling.** Online/offline from the status topic driving Aggregator liveness.
9. **E3.9 Listener liveness model.** Streaming/sleeping/offline states from Aggregator-reported liveness blocks, wake-window and grace handling (spec 6.5).
10. **E3.10 Command channel.** `POST /aggregators/{id}/commands` publishing to the cmd topic with `command_id` dedup.
11. **E3.11 Timeline and history.** Transition recording with actor and diff (spec 6.3), timeline API and UI panel.
12. **E3.12 Websocket live updates.** `WS /ws` channels for status and reconciliation transitions scoped by role.

### SIM: Simulation harness

Goal: a mock fleet so every later epic tests against realistic scale without hardware. Builds against the E3.3 contracts and can start from the spec alone once E0 exists.

Definition of done: one command launches a configurable fleet (target: 20 or more mock aggregators, around 30 listeners each) that connects to the broker, applies desired config, reports state, and exercises failure scenarios; CI runs a small fleet in the test suite.

1. **SIM.1 Mock Aggregator.** MQTT client implementing desired/reported/status/event per the contracts, config apply with reported ack.
2. **SIM.2 Mock Listener behavior.** Listener config handling via the parent mock Aggregator, liveness states, wake declarations and missed-wake events.
3. **SIM.3 Scenario scripting.** Injectable behaviors: apply errors, drift, disconnects, missed wake windows, duplicate MACs, unprovisioned `aggregator_uuid`.
4. **SIM.4 Fleet runner.** Parameterized launch at the spec 14.2 simulation scale on a single host.
5. **SIM.5 CI integration.** A small simulated fleet in integration tests; the full-scale run documented as a manual load check (feeds E8.6).

### E4: Provisioning tool

Goal: bundle generation in both modes, the device-facing encryption scheme, the bootstrap block, and provisioning tracking.

Definition of done: an operator generates a Pod bundle in the UI, downloads an archive containing the pod config file (with firmware-encrypted secrets), the Aggregator `settings.yaml` with bootstrap block, manifest, and README; provisioning records track generated through confirmed/mismatch as devices come online.

Cross-epic constraint: E4.6 (bootstrap block) consumes per-device broker credentials minted by E5.6, and spec 16.5 gates bundle generation on a verified broker. Schedule E5.1 through E5.6 before or alongside E4.6, or land E4 with the bootstrap block behind a flag until E5.6 merges.

1. **E4.1 Bundle and record data model.** `provisioning_bundle` and `device_provisioning_record` tables with the spec 8.5 status set.
2. **E4.2 Versioned device config template.** The on-card file format as a versioned template (spec 17 item 1 tracks the final firmware-agreed schema; build against the current draft).
3. **E4.3 Per-pod file generation.** The shared Pod file covering network settings, with blanks for later-filled per-listener data (spec 8.2 mode 1).
4. **E4.4 Per-listener file generation.** MAC-keyed files, folder plus manifest for a Pod or Deployment (spec 8.2 mode 2), mixed-mode support.
5. **E4.5 Device-facing envelope encryption.** Firmware KEK wrap of per-Pod DEKs, DEK encryption of PSK and stream key, embedded in the pod file (spec 8.4). Coordinate the KEK custody decision (spec 17 item 7) before shipping beyond dev.
6. **E4.6 Aggregator `settings.yaml` with bootstrap block.** `aggregator_uuid` generation and inventory recording, broker endpoint and per-device credentials from E5.6, plaintext per spec 16.4.
7. **E4.7 Manifest and README generation.** `manifest.json` with checksums and revisions, human-readable README (spec 8.3).
8. **E4.8 Export archive and download.** Bundle assembly and the download endpoint.
9. **E4.9 Registration matching and record transitions.** MAC-matched registration on first contact, flashed/registered/confirmed/mismatch transitions wired to E3's reported stream.
10. **E4.10 Provisioning wizard and tracking UI.** Mode selection per Pod, generation flow, record status board. (Follows DES.6 wireframes if available.)
11. **E4.11 Fill-in-later flow.** Cloud-side editing of name and GPS for registered Listeners (spec 8.6).

### E5: Deployment services onboarding

Goal: spec Section 16 end to end: both paths, connection tests, credential delivery.

Definition of done: an operator either enters existing service credentials and sees every test pass, or downloads a generated stack, runs it, and verifies it; a provisioned Aggregator receives all service configuration over MQTT on first connect; `services_status` reflects reality and degrades on failure.

1. **E5.1 Services data model.** Per-deployment service records with endpoints, encrypted credentials (via E0.11), per-service status and last-tested timestamps.
2. **E5.2 Write-only secrets API.** `GET/PUT /deployments/{id}/services` with secret fields accepted but never echoed.
3. **E5.3 Connection test framework.** `POST /deployments/{id}/services/test` running per-service testers with structured pass/fail detail.
4. **E5.4 Service testers.** The five testers from spec 16.2: MQTT pub/sub probe with dynsec detection, Influx query plus test-point write/delete, Prometheus read query plus remote-write receiver probe, Grafana health plus datasource check/provision plus webhook contact-point registration, S3 head-bucket plus test-object put/delete.
5. **E5.5 Services status lifecycle.** Rolled-up `services_status`, periodic re-checks, degradation handling (spec 16.5), status endpoint.
6. **E5.6 Per-device broker credential minting.** dynsec-based credential and ACL creation over MQTT; manual-install fallback with held-bundle state for non-dynsec brokers (spec 16.4). Unblocks E4.6.
7. **E5.7 Post-connect delivery wiring.** Deployment service settings flow into effective config and publish as retained desired state through the E3.7 worker; verify a cold-start device receives everything on first connect.
8. **E5.8 Stack generator: compose and config templates.** Templated `docker-compose.yml` plus per-service configs (Mosquitto with TLS and dynsec, Influx init, Prometheus web config and remote-write flag, Grafana provisioning, optional MinIO) per spec 16.3.
9. **E5.9 Stack credential generation and registration.** Platform-generated credentials written into the bundle and stored encrypted before download.
10. **E5.10 Stack bundle endpoints.** Generate and download endpoints, README with ports and run instructions.
11. **E5.11 Rotation and regeneration flow.** Regenerate with rotated credentials, re-verify, republish device config through the control plane.
12. **E5.12 Onboarding wizard UI.** Both paths, per-service test results, verify-services step, status display, bundle-generation gating on broker verification. (Follows DES.6 wireframes if available.)

### E6: Map and monitoring

Goal: the spatial and status view of the fleet.

Definition of done: the map renders the hierarchy with correct leaf-vs-pin behavior against a SIM fleet, rollup status and colors follow spec 9.3 including deployment services status, detail panels open with config, reconciliation state, and events, and live updates arrive over the websocket.

1. **E6.1 Map foundation.** Leaflet plus react-leaflet, OSM tiles, PMTiles source option behind the tile-source interface.
2. **E6.2 Hierarchy markers and clustering.** Deployment, Pod, and Listener rendering per spec 9.2 with marker clustering.
3. **E6.3 Leaf versus pin logic.** GPS-less Listeners clustered around their Aggregator, pin placement once coordinates fill in.
4. **E6.4 Status rollup engine.** Worst-of-descendants rollup combining online/offline, reconciliation state, alerts, and E5.5 services status.
5. **E6.5 Device detail panels.** Status, effective config, reconciliation state, recent events, provisioning record, Grafana link placeholder until E7.
6. **E6.6 Live map updates.** Websocket-driven status changes without reload.
7. **E6.7 Owner landing view.** Organization summary layout with per-deployment cards (data completed by E7).

### E7: Telemetry and alerts

Goal: read integration with each deployment's stores, the Owner rollup, and Grafana alert surfacing.

Definition of done: device panels show sparklines and embedded Grafana panels scoped by `aggregator_uuid`, the Owner summary shows the spec 10.3 metrics from cached fan-out, a firing Grafana alert appears on the correct device within seconds and resolves, backfill recovers alert state after platform downtime.

1. **E7.1 Influx read client.** SQL/FlightSQL queries scoped by `aggregator_uuid`, credentials from E5.
2. **E7.2 Prometheus read client.** PromQL queries scoped by `aggregator_uuid`, including the spec 10.5 metric families and queue-depth signals.
3. **E7.3 Telemetry endpoints and caching.** `GET /{entity}/{id}/telemetry` with short-TTL caching, sparkline sample cache.
4. **E7.4 Owner summary fan-out.** Cached cross-deployment rollup on an interval (spec 10.3), summary endpoints, stale-marking on unreachable deployments.
5. **E7.5 Grafana embeds.** Signed embed URLs scoped to the selected device, embed rendering in detail panels.
6. **E7.6 Alert webhook receiver.** `POST /webhooks/grafana-alerts`, alert state storage, label-to-hierarchy mapping (spec 11.2).
7. **E7.7 Alert backfill.** Pull current state from the Grafana API after downtime (spec 11.3).
8. **E7.8 Alert surfacing.** Map badges, panel indicators, filterable alert list, websocket channel for new alerts.
9. **E7.9 Outbound alert webhooks.** Forwarding hook for external notification systems.

### E8: Hardening and cloud

Goal: production readiness.

Definition of done: key rotation works end to end, the full-scale SIM run meets the spec 14 targets on one host, a deployment outage degrades gracefully, the stack deploys to Kubernetes from manifests, and the auth interface accepts an OIDC provider in a smoke test.

1. **E8.1 Platform key rotation.** Re-wrap of data keys under a new platform KEK (spec 12.4), secret-manager backend implementation.
2. **E8.2 Performance pass.** Index review, query tuning, cache TTL tuning against the spec 14.4 targets.
3. **E8.3 Degradation behavior.** Unreachable-deployment staleness handling verified end to end, alert and telemetry backfill after platform downtime (spec 14.3).
4. **E8.4 Kubernetes manifests.** Manifests/Helm for the cloud topology (spec 15.2).
5. **E8.5 OIDC interface.** Pluggable provider behind the auth interface with one reference provider smoke-tested.
6. **E8.6 Full-scale simulation load test.** The SIM.4 fleet at target scale against the complete platform; capture and fix findings.
7. **E8.7 Security review.** Headers, CORS, ACL audit, secret-leak scan of logs and API responses, dependency audit.

---

## 4. Dependency ordering

Start order and parallelism:

1. **Immediately, in parallel:** DES.1 through DES.3 and E0. The design track needs no code; E0 blocks everything else.
2. **After E0:** E1 starts. SIM can also start, building against the spec 7 contracts, and converges with E3 when E3.3 lands.
3. **After E1:** E2 and E3 run in parallel (E3 needs E1's inventory to target devices; E2 needs E1's entities to hang overrides on). E2.6 apply and E3.7 reconciliation meet in the middle: the full change-to-applied loop closes only when both exist.
4. **After E1 and E3:** E5 starts (its post-connect delivery and dynsec minting need the control plane).
5. **After E1 and E2:** E4 starts, except E4.6 waits on E5.6. If E5 lags, land E4 with the bootstrap block flagged off.
6. **After E1, E3 (and E5.5 for the services rollup):** E6.
7. **After E5 and E6:** E7 (needs verified service credentials to query, and the map to surface into).
8. **Last:** E8, plus DES.8.
9. **DES.7 (apply design system):** any time after DES.4/DES.5 and the E2 UI; before E6 is ideal.

Critical path: E0 → E1 → E3 → E5 → E7 → E8. E2, E4, SIM, E6, and all of DES hang off that path with slack, so they absorb parallel contributors without blocking the chain.

Cross-epic task-level dependencies worth encoding in Jira:

- E4.6 depends on E5.6 (broker credentials in the bootstrap block).
- E4.9 depends on E3.5 (registration matching consumes reported messages).
- E5.7 depends on E3.7 (delivery rides the reconciliation worker).
- E5.4 Grafana tester relates to E7.6 (it registers the contact point the webhook receiver consumes).
- E6.4 depends on E5.5 (services status in the rollup) and E3.8/E3.9 (liveness inputs).
- E7 as a whole depends on E5.1 through E5.5 (credentials and verification).
- SIM.5 feeds E8.6.
- DES.7 depends on DES.4, DES.5, and E0.4's token structure.
- E4.5 needs the spec 17 item 7 KEK custody decision before production use; it does not block dev implementation.

Open spec decisions that gate specific tasks (spec Section 17): item 1 (device config file format) gates finalizing E4.2; item 2 (local link framing) is firmware-side and does not gate platform work; item 3 (auto-reconcile policy) gates one behavior flag in E3.7; item 14 (dynsec requirement) gates whether E5.6's manual fallback ships.

---

## 5. Phase documents

Produce one phase document per implementation epic (E0 through E8, plus SIM). DES needs no phase document; it is a human design process, though DES.6 outputs feed the wizard-building epics. Each implementation session receives exactly two inputs: spec v1.1 and the phase document.

Each phase document contains:

1. **Scope.** The epic's goal and the spec sections it implements, stated as binding.
2. **Prerequisites and inherited interfaces.** What previous phases delivered and the exact interfaces this phase consumes (schemas, endpoints, topic contracts, the token structure). This section keeps a fresh conversation from reinventing or breaking earlier work.
3. **Out of scope.** Adjacent spec material this phase must not build, with the epic that owns it, to stop scope creep across sessions.
4. **Task list.** The epic's tasks from this document, expanded with acceptance criteria per task.
5. **Definition of done.** The epic-level definition from this document, plus required tests (the spec 14.5 test-critical components get explicit coverage requirements in E2, E3, E4, and E0's RBAC).
6. **Handoff artifacts.** What the phase must leave behind for later phases: migration state, documented interfaces, seeded dev data, and updates to a running `INTERFACES.md` in the repo.

Recommended authoring order: write the E0 phase document first and start implementation; write each subsequent phase document as its predecessor nears completion, folding in anything the finished phase changed. Keep the spec authoritative; when implementation forces a deviation, amend the spec version and note it in the next phase document rather than letting the documents drift apart.

> **Addendum PLAN-5-01 (2026-07-23, ref project-changes #3):** Superseded detail: each implementation session receives the spec, the phase document, and the current `docs/INTERFACES.md` (plus `docs/DECISIONS.md` once it has content) per implementation-handbook.md section 2, not "exactly two inputs".

---

*End of project development plan v1.0.*
