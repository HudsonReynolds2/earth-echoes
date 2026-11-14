# E4 Progress Ledger

Per-task state for epic E4 (Provisioning tool), maintained as the epic is implemented. The
phase document (`phase-4-provisioning.md`) is the binding scope; this file is the running
record of where the work is, so a session joining mid-epic does not have to reconstruct it
from git.

**Rule R0 governs the order.** A task is `gate green` only when `make gate` passed the ENTIRE
accumulated suite — 0 failed, 0 skipped, 0 xfailed, 0 deselected. No task starts before the
previous one is tagged. The implementing agent fills in its own row; the tag column is written
only after the commit and tag exist.

**Gate numbers are decided at tag time, not pencilled** (the E5 ledger's convention, adopted
here by hygiene batch 2 — see project-changes #41, DECISIONS D160). This column was originally
pencilled at 58-69, assuming SIM completed on gates 52-57; SIM in fact closed at gate-58, and
gates 59-63 went to E5's C3-C5, INFRA.1 and the E5↔SIM merge, with gate-64 to hygiene batch 2 —
the shift this note's earlier wording asked for, applied by retiring the pencils. Each task
takes `max(existing gate-* tags) + 1` when its tag is written and records the actual integer
here.

| Task | Status | Gate | Tag | Decisions | Notes |
|---|---|---|---|---|---|
| E4.0 Phase document and records | **gate green** | 66 | gate-66 | D166, D167, D168 | Phase doc, this ledger, project-changes entry, plan §3 addendum, spec §13 addendum. Docs only; the gate is a regression check. |
| E4.1 Bundle and record data model | **gate green** | 67 | gate-67 | D169, D170 | Migration `48467572e941`. `provisioning_bundle` (scope_type/scope_id + per-Pod `plan` JSONB, D170) and `device_provisioning_record` (`(target_type, target_id)` keyed like `config_revision`, `file_kind`/`file_path`, append-only `status_history`). Only `bundle_id` and `generated_by` are FKs. Vocabularies in `app.models`, re-exported from `app.provisioning`. `test_provisioning_model.py` 9 passed. D169: `test_migrations`' autogenerate check now imports `app.models` itself. |
| E4.2 Versioned device config template | **gate green** | 68 | gate-68 | D166 | `app/provisioning/device_config.py`: `render_device_config_v1` (pure, deterministic), `CARD_KEYS` derived from `CATALOG` (pod/listener/any, not write-restricted = spec 5.4 + network block), **no secret key emitted** (PHASE4-2-02), `TEMPLATE_VERSION = 1`, DRAFT on line 2. Golden `tests/golden/device_config_v1.yaml`. `test_device_config.py` 13 passed; leak scan proven falsifiable; all-digit MACs quoted (YAML 1.1 base-60 trap). **Note for E4.3:** the demo seed sets no `network.*` values, so demo Pods must be given network settings before a bundle can generate. |
| E4.3 Per-pod file generation | **gate green** | 71 | gate-71 | D173 | `app/provisioning/generate.py`: `generate_pod_file` resolves at the POD via `effective_resolved`, blanks `INVENTORY_KEYS`, refuses unset `network.*` (`GenerationError.missing`; WiFi password exempt on `OPEN`) and Aggregator-less Pods. Records embed each Listener's latest revision or NULLs (no revision ever created). Aggregator record deferred to E4.6 with its `settings.yaml` (D173). `test_pod_file_generation.py` 8 passed. |
| E4.4 Per-listener file generation | **gate green** | 72 | gate-72 | D174 | `generate_for_pod` (per_pod / per_listener / mixed), `generate_listener_file` (resolved at the Listener, nothing blanked), `generate_for_deployment` (per-Pod plan, Pods must belong to the Deployment). Out-of-Pod or unknown MACs and mode/list mismatches refused. Windows-legal paths (`listeners/02-EE-….yaml`). `test_listener_file_generation.py` 13 passed. |
| E4.5 Device-facing envelope encryption | **gate green** | 73 | gate-73 | D175 | `app/provisioning/envelope.py` (`FirmwareKey`, `wrap_dek`/`unwrap_dek`, `seal`/`unseal`, `seal_secrets`): AES-256-GCM, `base64(nonce||ct||tag)`, AAD-bound per field, one fresh DEK per Pod per export. Cards end with a `secrets:` block (golden moved by exactly that block). `EOE_FIRMWARE_KEK` required by `create_app` (not `Settings`), `EOE_FIRMWARE_KEY_VERSION` optional. Compose `api` fail-fast. 34 test `Settings(...)` sites gained `firmware_kek`. `test_firmware_envelope.py` 15 passed. **Spec 17 item 7 (KEK custody) stays open.** |
| E4.6 Aggregator `settings.yaml` + bootstrap | **gate green** | 74 | gate-74 | D167, D176 | `app/provisioning/bootstrap.py`: async `generate_settings_yaml`. `aggregator_uuid` from inventory; broker endpoint/TLS/CA via `coordinates_for`; device login reused if `minted`, else `mint_credential` via `app.state.credential_provider`. `EOE_BOOTSTRAP_CREDENTIALS` default ON; off writes `missing:`. `BootstrapRefused` (names the deployment) unless `services_status == 'verified'` and an MQTT row exists. Record `target_id=str(aggregator.id)` (D75). PEM as a literal block (found by the manual check). Endpoint is the platform's view of the broker (D176 limitation). `test_bootstrap_settings.py` 9 passed. |
| E4.7 Manifest and README | **gate green** | 75 | gate-75 | D177 | **TEST-CRITICAL, locked from gate 75.** `app/provisioning/manifest.py`: `bundle_members` (cards + README + manifest; duplicate paths refused), `build_manifest` (path/kind/pod/cards with revisions/sha256/redacted expected contents; `excludes: [manifest.json]`), `build_readme` (what goes where, DRAFT ×2, plaintext bootstrap, ACTION NEEDED when credentials off), `verify_members` (both directions + checksums; E4.8 runs it on every archive). `test_provisioning_manifest.py` 30 passed, table-driven over 5 bundle shapes, with negative tests proving the checker bites. |
| E4.8 Export archive and download | **gate green** | 76 | gate-76 | D178 | `app/provisioning/archive.py` (deterministic plain zip), `service.py::create_bundle` (all cards before any mint → members → `verify_members` → zip → SecretStore `bundle:{id}:archive` → rows; compensating delete), `app/api/provisioning.py`: POST/GET list/GET/GET download. Download = **MANAGE** (live broker logins). Bundles immutable; regeneration = new row. `E0_ROUTES` +4. `test_provisioning_api.py` 14 passed, suite-wide leak collector scanning DECOMPRESSED archives (deflate hides plaintext, measured). |
| E4.9 Registration matching and transitions | **gate green** | 77 | gate-77 | D168, D179 | `app/provisioning/records.py`: `derive_status` (pure read of `device_state`/`config_revision`/`quarantined_report`), `reconcile_records` (idempotent, rows diffed), `mark_flashed`. Confirmed latches; a later revision registers, never mismatches; only quarantines at/after the record count. Registered as the `provisioning-records` worker sweep — the fourth authorized E3 edit, registration only. `GET`/`PATCH /provisioning/records/{id}` (`flashed` only; 409 past generated). `test_provisioning_records.py` 10 + API 7 passed; **end to end against a live SIM mock Aggregator** in `sim/tests/test_provisioning_journey.py` (2 tests, 87s: two applies instead of four). |
| E4.10 Provisioning wizard and tracking UI | not started | — | — | — | S6 layout at v2 values; record status is NOT device status — no `StatusChip`, follow `.outcome-*`. |
| E4.11 Fill-in-later flow + walkthrough | not started | — | — | — | Spec 8.6 name/GPS via the E1 endpoints; `guide/e4-verification.md` and amendments to earlier walkthroughs. |

Batching, one PR per batch on `e4-batch-N` (rule R3; project-changes #43):
**B1** = E4.0-E4.2 · **B2** = E4.3-E4.5 · **B3** = E4.6-E4.8 · **B4** = E4.9-E4.11.
**Each batch is branched from `main` only after the previous batch's PR is merged** (owner's
decision at plan approval, 2026-11-13). Every task still ends on a full gate (D161).

## Notes for whoever picks this up next

- **E5 landed first, and fixed choice 1 has reversed.** See addendum PHASE4-2-01 on the phase
  document and DECISIONS D116/D117. `BrokerCredentialProvider` already exists, defined by E5.6
  in `backend/app/services/credentials.py`, with a dynsec and a dev-broker implementation, so
  E4.6 imports the protocol rather than declaring it and writes no dev provider of its own.
  dynsec is required for v1 (spec 17 item 14 closed), so there is no manual-install path to
  accommodate. And the degraded verified-broker predicate the E4.6 row above describes is **not
  needed** — `deployment.services_status` exists, so E4.6 gates on `services_status ==
  'verified'` directly.
- **Four owner decisions were taken at plan approval** and are recorded as fixed choices in
  the phase document section 2: the E4.6 flag-and-provider-seam, the YAML `device_config` v1
  draft, the env-KEK/AES-256-GCM firmware envelope, and the plain-zip archive. They resolve
  spec 17 items 1 and 7 and the spec 13 archive wording **for this epic's purposes only** —
  item 1 and item 7 themselves stay open, and the phase document says why. Do not relitigate
  them mid-epic; a task that seems to need one reopened is a stop-and-ask.
- **`effective_resolved` is the only path to plaintext config**, and it never touches an HTTP
  response. `effective_for` (redacted) is what routers may call; `effective_raw` (markers
  verbatim) belongs to revision snapshots. A card carrying `__secret__` instead of a PSK boots
  nothing, so the accessor choice is load-bearing.
- **E4.9 reads E3's tables; it does not edit `app/controlplane/consumer.py`.** That module is a
  published contract with a locked suite and deliberately delicate identity handling. A task
  that appears to need an edit there is a stop-and-ask (rule R2).
- **`aggregator_uuid` is already minted by E1** at inventory create time. E4.6 embeds and
  records the existing value. There is no second minting path, and building one would break the
  spec 4.2 join key.
- **E4.7's suite joins the four test-critical suites the moment it lands** (spec 14.5). After
  that, extending it is fine and weakening it is not, and any change to it needs a
  `DECISIONS.md` entry.
- **The two encryption schemes nest.** `SecretStore` protects secrets at rest under the platform
  KEK; the spec 8.4 envelope protects the same secrets in transit to a device under the firmware
  KEK. A value is plaintext only in memory, inside the generator, between the two.
- The four Aggregator-side secret keys (`upload.s3_*`, `telemetry.influx_token`,
  `telemetry.prom_remote_write_password`) **never enter a card file** — they arrive as retained
  desired config after first connect (spec 16.4). Only `network.wifi_password` and
  `network.stream_key` go through the firmware envelope.
- `MANAGE_PROVISIONING` and `VIEW_PROVISIONING` already exist in `app/auth/rbac.py` and
  `frontend/src/lib/rbac.ts`. This epic is their first consumer and changes neither map.
