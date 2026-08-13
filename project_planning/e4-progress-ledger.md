# E4 Progress Ledger

Per-task state for epic E4 (Provisioning tool), maintained as the epic is implemented. The
phase document (`phase-4-provisioning.md`) is the binding scope; this file is the running
record of where the work is, so a session joining mid-epic does not have to reconstruct it
from git.

**Rule R0 governs the order.** A task is `gate green` only when `make gate` passed the ENTIRE
accumulated suite — 0 failed, 0 skipped, 0 xfailed, 0 deselected. No task starts before the
previous one is tagged. The implementing agent fills in its own row; the tag column is written
only after the commit and tag exist.

**Gate numbers below are pencilled**, assuming SIM completes on gates 52-57 and nothing else
lands between. If anything does, shift the whole column and note it here — the sequence
matters, the specific integers do not.

| Task | Status | Gate | Tag | Decisions | Notes |
|---|---|---|---|---|---|
| E4.0 Phase document and records | not started | 58 | — | — | Phase doc, this ledger, project-changes entry, plan §3 addendum, spec §13 addendum. Docs only; the gate is a regression check. |
| E4.1 Bundle and record data model | not started | 59 | — | — | `provisioning_bundle`, `device_provisioning_record`, spec 8.5 statuses as a CHECK, un-FK'd evidence columns (D33/D55 precedent). |
| E4.2 Versioned device config template | not started | 60 | — | — | `device_config` v1, YAML, `schema_version: 1`, DRAFT-marked in three places. Golden-file pinned. |
| E4.3 Per-pod file generation | not started | 61 | — | — | Spec 8.2 mode 1. Byte-identical across the Pod; blanks derived from the catalog's `resolution="inventory"` keys. |
| E4.4 Per-listener file generation | not started | 62 | — | — | Spec 8.2 mode 2 plus mixed mode. |
| E4.5 Device-facing envelope encryption | not started | 63 | — | — | `EOE_FIRMWARE_KEK` + AES-256-GCM throughout; fresh per-Pod DEK per export; `app/provisioning/envelope.py`. |
| E4.6 Aggregator `settings.yaml` + bootstrap | not started | 64 | — | — | `BrokerCredentialProvider` seam, `EOE_BOOTSTRAP_CREDENTIALS` off by default, degraded verified-broker predicate. |
| E4.7 Manifest and README | not started | 65 | — | — | **TEST-CRITICAL (spec 14.5).** Complete in both directions. Its suite is locked from the moment it lands. |
| E4.8 Export archive and download | not started | 66 | — | — | Plain zip; `POST/GET /provisioning/bundles`, `GET .../download`; secret-leak scan over the archive and the responses. |
| E4.9 Registration matching and transitions | not started | 67 | — | — | Reconciler over `device_state`/`config_revision`/`quarantined_report`. `confirmed` latches; a later revision is not a mismatch. |
| E4.10 Provisioning wizard and tracking UI | not started | 68 | — | — | S6 layout at v2 values; record status is NOT device status — no `StatusChip`, follow `.outcome-*`. |
| E4.11 Fill-in-later flow + walkthrough | not started | 69 | — | — | Spec 8.6 name/GPS via the E1 endpoints; `guide/e4-verification.md` and amendments to earlier walkthroughs. |

Suggested batching, one PR per batch on `e4-batch-N` (rule R3):
**B1** = E4.0-E4.2 · **B2** = E4.3-E4.5 · **B3** = E4.6-E4.8 · **B4** = E4.9-E4.11.

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
