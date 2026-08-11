# Phase 3 Document: Control Plane and Reconciliation (Epic E3)

**Companion documents:** Technical Specification v1.1 (authoritative), Project Development Plan v1.0
**Spec sections implemented:** 6, 7
**Depends on:** E0 and E1 complete. Runs in parallel with E2; the full change-to-applied loop closes only when both phases exist.

---

## 1. Scope

Build the device control plane: the Mosquitto integration, the spec 7.2/7.3 topic contracts as typed schemas, retained desired publication, the reported consumer, the config revision state machine, the reconciliation worker, LWT and Listener liveness handling, the command channel, the per-device timeline, and websocket live updates. When this phase ends, publishing a config change drives a mock Aggregator from pending to applied, drift and timeouts transition correctly, and the UI shows it live.

## 2. Prerequisites and inherited interfaces

From E0 (read `docs/INTERFACES.md` first): API conventions, RBAC dependency (commands and publishes require `deployment_operator` within scope), audit hook, `SecretStore` (broker credentials live behind it), migration conventions, websocket-ready frontend shell.

From E1: the entity schema, the deployment `slug` (the `{dep}` topic segment; never use the name or UUID in topics), the three Aggregator identity columns, and the E1.5 service interfaces for report-time identity handling (`duplicate_identity` quarantine and the `provisioning_required` membership check). E3.5 wires live messages into those services; do not reimplement their logic.

Coordination with E2 (parallel): E2 owns the settings catalog, overrides, the merge engine, and creating `config_revision` rows in `draft` via apply. This phase owns everything after draft. The `config_revision` table shape is recorded in `docs/INTERFACES.md` (defined by whichever phase lands first; the Phase 2 document carries the default: id, target entity type and id, effective-config snapshot JSONB, checksum, state, created_by, created_at). This phase adds the state transitions and a `publish_revision(revision_id)` entry point that E2's apply calls when `EOE_PUBLISH_ENABLED` is on. If E2 has not landed when the worker needs effective-config recomputation, consume revision snapshots as published and integrate the merge-engine call when E2 merges.

Coordination with SIM: the E3.3 contracts module is the interface SIM builds against. This phase writes only the minimal in-test mock fixtures its own integration tests need; the fleet-scale harness, scenario scripting, and runner belong to SIM.

Fixed choices for this phase:

- **Client library:** aiomqtt (spec 3.2 allows aiomqtt or paho; pick aiomqtt and stay async end to end).
- **QoS and retention:** QoS 1 on all platform topics; retained flags exactly per the spec 7.2 table (desired and status retained, reported/event/cmd not).
- **Payloads:** JSON with top-level `schema_version: 1`, shapes per spec 7.3 including the Listener liveness block and the `listener_missed_wake_window` event.
- **Contracts location:** `backend/app/contracts/mqtt.py` (Pydantic models plus topic-string builders). SIM imports this module; treat it as a published interface from the moment it merges.
- **Broker connection storage:** create the `deployment_services` table now, populated with the MQTT entry only (host, port, TLS settings, platform account credentials via `SecretStore`). E5 owns extending this table to the other services, connection tests, and the status lifecycle; this phase only defines the broker row shape and reads it. Record the shape in `INTERFACES.md` explicitly marked as E5's to extend.
- **Timeouts:** pending-revision timeout defaults to 300 seconds, configurable per deployment (a platform setting, not a device setting).
- **Dev TLS:** a script generates a local CA and certs into a gitignored `deploy/dev-certs/`; the dev broker and client both use them so TLS paths are exercised from day one (spec 7.1).

> **Addendum PHASE3-2-01 (2026-08-10, ref project-changes #19):** The coordination contingency above is moot: E1 and E2 both completed and merged (gates 20-38) before this phase started. `config_revision` therefore arrives exactly as E2 defined it (DECISIONS D55) rather than being defined here; the merge engine is available to the reconciliation worker from E3.7 with no snapshot-only fallback; and E3.13 is in scope for this phase rather than deferred. Owner decisions taken at plan approval: DECISIONS D59 (one worker module with two entrypoints, and a Postgres LISTEN/NOTIFY bus for websocket fan-out because spec 3.2/15.1 keep Redis optional), D60 (this phase lifts D40, on real reported state only), and D61 (`EOE_PUBLISH_ENABLED` defaults on at E3.13).

## 3. Out of scope

The settings catalog, merge engine, overrides, preview, and apply endpoints (E2). The fleet-scale simulation harness, scenario scripting, and runner (SIM). Provisioning bundles, the bootstrap block, and per-device broker credential minting (E4/E5; dev devices in this phase authenticate with dev credentials created by the dev-broker script). Service connection tests, the onboarding flow, and the rest of the `deployment_services` model (E5). Map rendering (E6). Alert storage and surfacing beyond persisting device events (E7 owns alerts; the Grafana webhook is not this phase). Balena API integration of any kind (spec 7.5 bounds it; nothing here needs it). Listener-side local-link mechanics (firmware territory; spec 17 item 2).

## 4. Task list

**E3.1 Broker for development.** Mosquitto in the dev compose stack with a TLS listener, the platform account, and per-device dev credentials plus topic ACLs restricting each device to its own subtree (spec 7.1), all provisioned by the dev-cert/credential script. Acceptance: an ACL test proves one dev device cannot read another's subtree; the platform account can reach the whole deployment namespace.

**E3.2 MQTT client manager.** An async manager owning one connection per deployment broker, loading connection details from the `deployment_services` MQTT row, with reconnect and backoff, clean startup/shutdown, and subscription registration. Acceptance: kill and restart the dev broker under test; the manager reconnects and resubscribes without message-handling code noticing.

**E3.3 Topic contracts and schemas.** The spec 7.2 namespace as topic-builder functions and the spec 7.3 payloads as Pydantic models with `schema_version`: desired config, reported Aggregator state, reported Listener state with the liveness block, LWT status, events (including `listener_stream_gap` and `listener_missed_wake_window`), and commands with `command_id`. Acceptance: round-trip serialization tests per model; topic builders reject bad slugs and MACs; the module docstring names SIM as a consumer.

**E3.4 Desired publish path.** `publish_revision(revision_id)`: snapshot the revision, publish retained to the target's desired topic (Aggregator or Listener subtopic per spec 7.2), transition draft to pending, audit the actor. Acceptance: the retained message is delivered to a subscriber that connects afterward (the spec 6.4 reconnect property); republish of the same revision is idempotent.

**E3.5 Reported consumer.** Subscribe to reported and event topics across deployments; treat reported messages as idempotent by `applied_revision_id` plus checksum and tolerate out-of-order delivery by ignoring stale reports (spec 7.4); route identity through the E1.5 services so `duplicate_identity` quarantines and `provisioning_required` fires off the membership check rather than touching inventory; persist device events. Acceptance: replayed and reordered message tests; a conflicting MAC report lands in quarantine with inventory rows provably unchanged.

**E3.6 Revision state machine.** All spec 6.2 states and transitions (draft, pending, applied, drifted, failed, superseded) as a single authoritative module, invalid transitions rejected. This is test-critical (spec 14.5): table-driven tests cover every legal transition, every illegal one, and the superseded path when a newer revision preempts a pending one. Acceptance: the transition table in tests matches spec 6.2 line for line.

**E3.7 Reconciliation worker.** The spec 6.4 loop: publish on change, compare reported to desired and advance state, time out pending revisions after the configured window into failed, and periodically re-compare applied devices for drift. Drift never auto-republishes in this phase: re-publish happens on operator action, with a per-deployment `auto_reconcile` flag stored but default off and inert pending the spec 17 item 3 decision. Acceptance: integration test drives a mock device through publish, ack, drift injection, operator re-publish, and timeout-to-failed; the worker survives restart without losing state (Postgres and retained messages hold it, spec 14.3).

**E3.8 LWT status handling.** Consume the retained status topic; LWT `offline` and explicit `online` drive the Aggregator's real-time online state, which is authoritative for the live verdict (spec 9.3). Acceptance: dropping a mock device's connection without a clean disconnect flips it offline via LWT under test.

**E3.9 Listener liveness persistence.** Persist the Aggregator-reported Listener liveness (streaming, sleeping with `expected_wake_at`, offline) and the `listener_missed_wake_window` events. The Aggregator computes grace and raises the event (spec 6.5); the platform stores and surfaces, never recomputes wake schedules. Acceptance: a sleeping Listener reads as healthy; a missed-wake event flips it offline; state transitions land on the timeline.

**E3.10 Command channel.** `POST /aggregators/{id}/commands` for restart, resync, and flush buffer, publishing to the cmd topic with a generated `command_id` for device-side dedup (spec 7.4), RBAC-gated and audited. Acceptance: duplicate submission of one logical command carries distinct `command_id`s; the mock device dedup test passes.

**E3.11 Timeline and history.** A `reconciliation_events` record per transition with timestamp, actor (user or system), before/after effective-config diff, and device-supplied detail (spec 6.3); `GET /{entity}/{id}/timeline` and a device timeline UI panel; the org and per-deployment views reuse the E0 audit surface filtered by scope. Acceptance: the E3.7 integration test's full journey renders as a coherent timeline in the UI.

**E3.12 Websocket live updates.** `WS /ws` channels for device status changes and reconciliation transitions, scoped by the caller's role and deployment assignments (spec 13); the frontend inventory and timeline views consume them. E7 adds the alerts channel later; leave the channel registry open for it. Acceptance: two browser sessions with different scopes receive correctly filtered events from one underlying change.

**E3.13 Close the loop with E2.** When both phases have merged: wire E2's apply to `publish_revision`, flip `EOE_PUBLISH_ENABLED` default on, and run the end-to-end test (edit config in UI, preview, apply, mock device applies, state reaches applied, timeline and websocket reflect it). Acceptance: that end-to-end test lives in CI against a small in-test mock fleet.

> **Addendum PHASE3-4-02 (2026-08-10, ref project-changes #22):** E3.7 additionally ships `POST /revisions/{revision_id}/publish` (the operator action this task's own "re-publish happens on operator action" requires, and which no other E3 task claimed), a `worker` service in the dev compose stack, and a publish-only broker connection in the API process. It also adds `deployment.pending_timeout_seconds` / `deployment.auto_reconcile` and `config_revision.published_at`. Nothing moves between tasks: E3.13 still owns wiring E2's bulk apply to publication and flipping `EOE_PUBLISH_ENABLED` on (DECISIONS D80-D86).

> **Addendum PHASE3-4-01 (2026-08-10, ref project-changes #20):** Two sequencing changes inside this phase, neither altering what it delivers. (1) `backend/app/contracts/mqtt.py` is created by E3.1 carrying the section 7.2 topic builders, because E3.1's broker ACL grants ARE topic strings and generating them from literals for two tasks would put the namespace in two places (DECISIONS D62); E3.3 adds the section 7.3 payload models to the same module. (2) Batch 2 runs E3.6 before E3.4 and E3.5, because both the publisher and the reported consumer transition revision state and the authoritative transition module must exist before either calls it. All thirteen tasks still ship, each with its own gate.

## 5. Definition of done

A config change published to a mock Aggregator lands as a retained message, the device ack drives pending to applied, injected divergence drives applied to drifted, silence past the window drives pending to failed, and a newer revision supersedes correctly, all visible on the device timeline and pushed over the websocket. LWT flips Aggregator status in real time; Listener liveness follows spec 6.5 semantics from reported data. Identity conflicts quarantine through the E1.5 services. The state machine's test suite matches spec 6.2 exactly. With E2 merged, the full UI-to-device loop runs in CI and `EOE_PUBLISH_ENABLED` defaults on.

## 6. Handoff artifacts

- `docs/INTERFACES.md` updated with: the contracts module path and its published status (SIM consumer), the `deployment_services` broker-row shape marked as E5's to extend, `publish_revision` and the flag contract with E2, the pending-timeout setting, the `auto_reconcile` flag and its inert status pending spec 17 item 3, the reconciliation_events shape, and the websocket channel registry (E7 adds alerts).
- The dev broker script (certs, platform account, dev device credentials, ACLs) documented in the README.
- `docs/DECISIONS.md` updated with any deviations, explicitly including the resolution reached with the E2 session on `config_revision` if it differed from the Phase 2 default.
