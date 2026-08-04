# Phase 2 Document: Configuration Model (Epic E2)

**Companion documents:** Technical Specification v1.1 (authoritative), Project Development Plan v1.0
**Spec sections implemented:** 5
**Depends on:** E1 complete. Runs in parallel with E3.

---

## 1. Scope

Build the settings catalog as versioned data, sparse per-entity overrides, the effective-config merge engine, the selection and query machinery, preview, and the schema-driven config editing UI. When this phase ends, an operator can set any spec 5.3 setting at any level, see exactly which devices a change affects and what their resulting effective config will be, and commit it, with actual publication to devices arriving in E3.

## 2. Prerequisites and inherited interfaces

From E0: API conventions, RBAC dependency (config writes require `deployment_operator` within scope), audit hook, `SecretStore` (secret-flagged settings store references through it, never raw values in overrides), frontend tokens.

From E1 (read `docs/INTERFACES.md`): the entity schema and foreign keys (the merge walks Organization → Deployment → Pod → Aggregator → Listener through them), the tag storage model (the selection engine queries it), and the demo fixture (use it in tests).

Coordination with E3 (parallel): E3 owns `config_revision` publication and the state machine. This phase creates revision rows in `draft` state only (see E2.6). Agree the `config_revision` table shape with the E3 session early, or if E3 has not started, define it here per spec 6.2 and record it in `docs/INTERFACES.md` for E3 to inherit: id (UUID), target entity type and id, snapshot of effective config (JSONB), checksum, state (enum from spec 6.2), created_by, created_at.

Fixed choices for this phase:

- **Catalog storage.** The spec 5.3 catalog is data: a `settings_catalog` table (or versioned JSON seed loaded into it) with key, type, enum values, range, default, lowest level, secret flag, notes, and a catalog `version` (start at 1). The seed file lives in the repo and matches spec 5.3 exactly, including the deployment service settings rows added in v1.1.
- **Override storage.** One `entity_overrides` row per entity holding a sparse JSONB map validated against the catalog on write. Secret-flagged keys store a secret reference (from `SecretStore`), never plaintext.
- **Selection queries are structured JSON, not a text DSL.** Shape: `{"entity_type": "listener", "scope": {"deployment_id": "..."}, "where": {"all": [{"setting": "audio.sample_rate_hz", "op": "eq", "value": 48000}, {"tag": "coastal"}]}}` with `all`/`any` nesting and ops `eq`, `ne`, `in`, `exists`, `tag`. This is the spec 5.2 mechanism; keep the grammar small and documented.

## 3. Out of scope

Publishing revisions to devices, the reported side, drift, timelines, and everything MQTT (E3). The provisioning bundle's consumption of effective config (E4). Writing deployment service settings through the onboarding flow (E5 owns that flow; this phase only ensures the catalog rows exist and merge correctly). Grafana, telemetry, alerts (E7). Restyling beyond tokens (DES).

## 4. Task list

**E2.1 Versioned settings catalog.** The catalog table plus seed exactly matching spec 5.3, load-on-migrate, and a read endpoint the frontend renders editors from. Acceptance: seed matches the spec table key for key (a test asserts it); unknown keys in overrides are rejected with the offending key named.

**E2.2 Sparse override storage.** `entity_overrides` with catalog validation (type, enum, range, level: writing a key below its allowed level is permitted per spec 5.3's inheritance note, writing an unknown key is not), secret-reference handling for secret-flagged keys. Acceptance: validation tests per type class; secret values round-trip through `SecretStore` and never appear in responses or logs.

> **Addendum PHASE2-4-01 (2026-08-04, ref project-changes #17):** The level clause above inverts spec 5.3, which permits setting keys *higher* than their lowest level, not lower — and spec 5.1's shared-network rationale argues directly against below-level writes. The shipped validator (owner decision 2026-08-04; DECISIONS D50) enforces at-or-above: a key is writable at its lowest level or any ancestor level, rejected below it with a 422 naming the key; `lowest_level='any'` is writable everywhere. The table ships as `entity_override` (singular, D30 convention).

**E2.3 Effective-config merge engine.** The spec 5.1 deep merge, Organization down to the target entity, later levels winning, deterministic and side-effect free. This is test-critical (spec 14.5): cover empty levels, partial overrides, full shadowing, secret refs, and merge at every entity level, property-based tests encouraged. Acceptance: the test suite for this module alone documents the merge semantics.

**E2.4 Effective and override endpoints.** `GET /{entity}/{id}/config/effective` (computed, with per-key provenance showing which level set it) and `GET/PUT /{entity}/{id}/config/overrides` (spec 13). Acceptance: provenance is returned per key; PUT validates through E2.2 and audits.

**E2.5 Selection engine.** The structured query above evaluating over inventory, overrides, effective values, and tags; `POST /selections/preview` returning the matched set; `GET/POST /selections` for saved queries (spec 5.2, 13). Effective-value predicates may evaluate via the merge engine per candidate; optimize only if the demo-fixture scale demands it. Acceptance: query tests spanning tag, override, and effective-value predicates with `all`/`any` nesting; saved selections re-evaluate at use time, not storage time.

**E2.6 Bulk preview and apply.** `POST /config/preview` takes a selection plus proposed changes and returns, per affected device: identity, changed keys, and before/after effective config (spec 5.2 requires an explicit preview before commit; spec 14.4 wants it computed server-side and streamed for large sets, so paginate or stream the response). `POST /config/apply` writes the overrides and creates `config_revision` rows in `draft` state, then stops: publication is E3's, and until E3 lands, apply returns the revision ids with `state: draft` and a documented note that a feature flag (`EOE_PUBLISH_ENABLED`, default off) gates the eventual publish call-through. Acceptance: preview matches what apply then produces; apply is transactional across the selection; the flag exists and is off.

**E2.7 Schema-driven config editor UI.** Per-entity config view rendering editors from the catalog (type, enum, range, secret masking), showing effective values with per-key provenance and a clear set/inherited/overridden distinction, and editing the local override map. Acceptance: adding a new catalog row (test key) renders an editor with no frontend change (spec 5.3's "catalog is data" requirement demonstrated).

**E2.8 Bulk edit UI.** Selection builder over the E2.5 query shape (with plain checkbox multiselect on device tables as the simple path, spec 5.2), the affected-device preview table, and the commit flow ending at draft revisions. Follow DES.6 wireframes if the design track has produced them; otherwise build functional and neutral. Acceptance: a user selects "all Listeners tagged X in deployment Y", previews, commits, and sees draft revisions listed.

## 5. Definition of done

Setting a value at any level changes descendants' computed effective config with correct provenance. Preview shows the exact affected set and resulting configs before any write. Tag and attribute selection works, saved and ad hoc. Apply produces draft revisions atomically and nothing publishes anywhere. The merge engine's test suite passes and reads as documentation. The catalog seed equals spec 5.3. Secrets never leave `SecretStore` in plaintext.

## 6. Handoff artifacts

- `docs/INTERFACES.md` updated with: the catalog table and seed location, the override storage shape, the merge engine function signature and module path (E3's reconciliation worker and E4's bundle generator both call it), the selection query grammar, the `config_revision` table shape and the draft-only contract plus the `EOE_PUBLISH_ENABLED` flag (E3 flips it), and the preview response shape.
- `docs/DECISIONS.md` updated with any deviations.
