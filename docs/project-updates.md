# Project Updates

## 2026-08-04: E2.7 schema-driven config editor (Gate 37 GREEN)

- **Tasks closed:** E2.7 (branch `e2-batch-3`; DECISIONS D57).
- **Gate:** 37 — first full run RED on `frontend: eslint` (Prettier formatting across
  the ten new/touched files; my standalone eslint check hadn't run the format half of
  the lint script). `prettier --write`, rerun GREEN. Three test-authoring fixes on the
  pre-gate vitest run (ambiguous text matches scoped to the chip class; a missing
  cleanup() between renders) — all test-side, no production changes.
- **Tests:** backend 357, vitest 88 (+28: config-editor 10, config-secrets 3,
  config-rbac 5, config-lib 9, shell +2 rows, inventory-levels +1), Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** the /configuration editor — shared-tree layout (`lib/hierarchy.ts`
  extraction; InventoryLayout repointed, behavior-neutral), five level routes incl.
  the aggregator's own, ContextBar tabs (first consumer; Settings/Tags/Revisions,
  S3's five folded per D57), the S3 provenance table on the .data-table vocabulary
  (quiet/loud rows, group headers with rationale captions, U+00D7 revert), catalog-
  driven editors for every type (bool = new ink-track ToggleSwitch; schedule = raw
  JSON v1; unknown types fall back safely), the **test-key acceptance** (fixture-only
  `test.demo_knob` grows a working editor, gate-pinned), secret rows (bullets,
  write-only Replace, diff says "replaced", plaintext-never-in-DOM test), local
  staging with the draft banner/diff and ONE wholesale PUT, per-key 422 landing,
  the inheritance chain with live ancestor counts, Field-Tech/viewer LOCKED treatment
  (aria-described), the D40 zero-[data-status] guard extended to config routes,
  Publish rendered disabled naming E3, and the ListenerDetail effective-config card
  with its Edit deep-link. Tokens: `--eoe-color-warning-border`,
  `--eoe-width-configrail` (+dark values same commit). The E1 shell Configuration
  page is deleted; shell.test's route table updated in the same commit.
- **Manual verification:** the component suites ARE the walk at this gate (the
  test-key acceptance, provenance walk, secret discipline, and RBAC treatments all
  assert against rendered DOM over the MSW fixture's real miniature merge); the
  full in-browser walkthrough is gate 38's deliverable (guide/e2-verification.md)
  per the E1 gate-27/28 precedent, honestly recorded here rather than claimed early.

## 2026-08-04: E2.6 bulk preview/apply + revisions read (Gate 36 GREEN)

- **Tasks closed:** E2.6 (branch `e2-batch-2`; DECISIONS D55-D56; project-changes #18
  with addendum PHASE2-4-02). This closes e2-batch-2 (E2.4-E2.6) — PR next.
- **Gate:** 36, GREEN — **the pre-gate run caught a real security defect**: the first
  plan implementation merged raw change values into the after-state, which put a
  secret's PLAINTEXT into the revision snapshot (storage itself was safe — only the
  snapshot leaked). The fix models secret changes as storage holds them (markers +
  keep-sentinel resolution) in the plan builder; the failing test now guards the
  invariant forever. Full gate green after the fix.
- **Tests:** backend 357 (+13), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `config_revision` table (migration `e8f61ab39c17`; per-device, un-FK'd,
  marker snapshots, state-string draft-only — the E3 handoff, published verbatim in
  INTERFACES). `app/config/plan.py` — ONE plan builder behind both endpoints (the
  preview==apply parity guarantee), write-at-level with common-ancestor resolution
  (422 naming candidates on a split; org level needs an org-wide grant), honest
  blast-radius device enumeration, no_op flagging. `POST /config/preview` (paginated,
  redacted, no CSRF) and `POST /config/apply` (one transaction: merged overrides +
  draft revisions + one config.apply audit row per affected deployment; response
  reports state draft + the inert `EOE_PUBLISH_ENABLED`). Revisions read surface
  (`app/api/revisions.py`): per-device lists (D7, -created_at, state filter,
  identical-404) + the snapshot-bearing item route. `EOE_PUBLISH_ENABLED` joined
  Settings + deploy/.env.example (the pairing test enforces both). Readiness locks:
  E0_ROUTES 65→70, E0_TABLES 16→17.
- **Manual verification:** ephemeral database + demo fixture over real HTTP — previewed
  a coastal-tag selection change (1 device, correct changed_keys, no_op false), applied
  (state draft, publish_enabled false, 1 revision), listed the listener's revisions
  (draft, -created_at), fetched the item (snapshot carries the new value; checksum
  `sha256:`-prefixed).

## 2026-08-04: E2.5 selection engine (Gate 35 GREEN)

- **Tasks closed:** E2.5 (branch `e2-batch-2`; DECISIONS D54).
- **Gate:** 35, GREEN — first full run (pre-gate check surfaced five long-line lint
  findings, auto-formatted, and four mypy findings from a reused statement variable,
  renamed per branch)
- **Tests:** backend 344 (+14), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/config/selection.py` — the spec-5.2 grammar as Pydantic models
  (all/any nesting; tag/eq/ne/in/exists/`ids` predicates; depth 5 / 50-predicate caps;
  secret value queries rejected, `exists` allowed) and the evaluator: SQL prefilter +
  in-Python predicates through the pure merge engine with batch-loaded chains (constant
  query count), ALWAYS re-filtered through the caller's visible deployments.
  `selection` table (migration `d1e53fa27b06`) stores the validated query verbatim —
  re-evaluated at every use, never a materialized id list. `app/api/selections.py`:
  POST /selections/preview (VIEW_STATUS, D7 envelope, deterministic order),
  GET /selections, POST /selections (CSRF + MANAGE_CONFIG-anywhere, 409 on duplicate
  names, audited) — GET/POST only per spec 13, no PATCH/DELETE (D54). Readiness locks:
  E0_ROUTES 62→65, E0_TABLES 15→16.
- **Manual verification:** ephemeral database + demo fixture over real HTTP —
  `{"tag": "coastal"}` matches exactly the fixture's one coastal-tagged listener
  (`alder-creek-01`); the default-sample-rate value predicate matches all 28.

## 2026-08-04: E2.4 effective and override endpoints (Gate 34 GREEN)

- **Tasks closed:** E2.4 (branch `e2-batch-2` opens batch 2 of the E2 plan).
- **Gate:** 34, GREEN — first full run (four long-line lint findings auto-formatted on
  the pre-gate check)
- **Tests:** backend 330 (+12), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/api/entity_config.py` — fifteen endpoints (GET effective, GET/PUT
  overrides × five entities) over three shared helpers; E1's scope discipline verbatim
  (org any-role read / org-wide-grant write, deployment 403-before-lookup,
  pod/aggregator/listener identical-404 — D35); every response redacted; PUT folds all
  validation errors into one 422 `validation_error` with detail.errors, staging
  nothing. Audit `config.override_update` carries set/unset KEY NAMES + catalog_version,
  never values. The four E1 DELETE endpoints now call `delete_overrides_for` and delete
  orphaned config secrets after their commit (D51 ordering). Readiness locks:
  E0_ROUTES 47→62.
- **Manual verification:** ephemeral database + demo fixture over real HTTP — owner
  session PUT wifi ssid + secret password on "Pod 01 · Alder Creek" (200, plaintext
  absent from the response body); `alder-creek-03` effective shows the ssid with
  source "pod", the password as the keep sentinel, and `identity.name` from inventory.

## 2026-08-04: E2.3 effective-config merge engine — test-critical suite locked (Gate 33 GREEN)

- **Tasks closed:** E2.3 (branch `e2-batch-1`; DECISIONS D52-D53). This closes
  e2-batch-1 (E2.1-E2.3) — PR next.
- **Gate:** 33, GREEN — the pre-gate check caught one real semantic failure first:
  the engine handed container values out BY REFERENCE, so mutating a result reached
  back into the chain. The locked suite's side-effect-freedom case is the documented
  semantic, so the ENGINE was fixed (containers copied on the way out), not the test.
  Full gate green after that fix plus minor lint formatting.
- **Tests:** backend 318 (+37: the locked merge suite 26 + service walk 9 + property
  extras), vitest 60, Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/config/merge.py` (pure core: effective_config /redact_secrets/
  resolve_secret_refs; deepest-setter-wins, wholesale replacement, inventory keys
  listener-only, defensive reads, no input aliasing — D53).
  `app/config/canonical.py` (the FROZEN checksum recipe: sorted-keys compact UTF-8
  JSON, `sha256:` prefix, markers included — D52; three golden digests pinned).
  `app/config/service.py` (ancestry via E1 FKs, one-query chain load, three accessors
  by audience: effective_for REDACTED for routers, effective_raw for E2.6 snapshots,
  effective_resolved INTERNAL ONLY for E3/E4). `hypothesis` joins the dev group
  (owner-approved) under a derandomized `gate` profile in conftest —
  `test_config_merge.py` is now one of the four suites no session may weaken, holding
  15 documented example semantics, 4 property cases, the golden checksums, and (in
  `test_config_service.py`) the JSONB round-trip stability guard.
- **Manual verification:** fresh ephemeral database + `seed_demo_hierarchy`: set
  `audio.sample_rate_hz=96000` on Redwood Coast, and `alder-creek-01` resolves it with
  source "deployment"; `identity.name` resolves from the listener row with source
  "inventory"; unset secret renders None; back-to-back checksums identical.

## 2026-08-04: E2.2 sparse override storage (Gate 32 GREEN)

- **Tasks closed:** E2.2 (branch `e2-batch-1`; DECISIONS D50-D51; project-changes #17
  with addendum PHASE2-4-01).
- **Gate:** 32, GREEN — first full run (three lint findings and one mypy finding on the
  pre-gate check were fixed before the gate; the gate itself passed first try)
- **Tests:** backend 281 (+19), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `entity_override` table (migration `c9d42be17a05`; singular per D30 —
  one sparse JSONB map per entity, UNIQUE(entity_type, entity_id), un-FK'd untyped
  entity_id per the audit precedent). `app/config/validation.py` — pure per-key
  validation, every error naming its key, all errors at once: unknown key, inventory-
  resolved (points at PATCH /listeners), service-restricted (names E5), the
  **owner-resolved level rule** (at-or-above lowest level, never below — the phase doc's
  inverted sentence recorded as deviation #17/PHASE2-4-01/D50), type/enum/range per
  class, null-never-a-value, 2 KiB object cap, bool-is-not-int. `app/config/overrides.py`
  — get/put (wholesale replace)/delete_overrides_for; secret keys store the
  `{"$secret": "config:..."}` marker with plaintext in SecretStore under the new
  `config:` namespace (flagged E0-contract extension, D51), keep-sentinel round-trip,
  post-commit deletion protocol. Readiness locks: E0_TABLES 14→15; the SecretStore
  round-trip gains both config name shapes (incl. the colon-bearing MAC form).
  INTERFACES gains the override-storage contract.
- **Manual verification:** fresh ephemeral database — secret put through the service:
  row holds only the marker, plaintext absent from the stored JSON, SecretStore
  round-trips the value under `config:pod:{id}:network.wifi_password`; below-level
  write rejected with the spec-citing message naming the key.

## 2026-08-04: E2.1 versioned settings catalog (Gate 31 GREEN)

- **Tasks closed:** E2.1 (branch `e2-batch-1`; epic E2 opens per the approved plan —
  owner decisions of 2026-08-04 recorded in DECISIONS D47-D49).
- **Gate:** 31, GREEN — first run
- **Tests:** backend 262 (+7), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/config/` package opens (the inventory-pattern: pure core beside
  DB service). `app/config/catalog.py` — the 37-row spec-5.3 `CATALOG` constant
  (`CATALOG_VERSION = 1`, merge-order `LEVELS`) and the upsert-plus-prune
  `seed_catalog()`. Migration `b7c31a90d2e4` creates `settings_catalog` (closed-vocab
  CHECKs) and seeds it in-migration; replays converge on the constant (D47).
  `GET /config/catalog` (any assignment) serves the schema document `{version, items}`
  sorted by key — deliberately not a D7 list (D47). Catalog facts: 6 secret rows, 12
  service-restricted rows (`telemetry.*` + 4 S3 keys; `upload.s3_prefix` deliberately
  writable, owner ruling — D48), 4 inventory-resolved rows (`location.*` + `identity.*`,
  D49). `test_settings_catalog.py` pins constant↔spec (hardcoded 37-key list) and
  constant↔table (field for field), seed idempotence/convergence, endpoint shape/sort,
  and the auth matrix. Readiness locks extended in-file: E0_ROUTES 46→47,
  E0_TABLES 13→14. INTERFACES gains "Owned by E2" (catalog contract + evolution rule).
- **Manual verification:** fresh ephemeral database migrated from scratch — 37 rows at
  version 1 (6 secret / 12 restricted), first/last keys eyeballed against the spec
  table; unauthenticated `GET /config/catalog` → 401. Migration up/down with data
  re-proven by the readiness replay inside the gate.

## 2026-08-04: Rules 1.1.0 — walkthrough currency joins R1 (Gate 30 GREEN)

- **Tasks closed:** the owner-directed rules amendment (branch `rules-batch-1`;
  DECISIONS D45). Docs/rules only; no production code or test changes.
- **Gate:** 30, GREEN
- **Tests:** backend 255, vitest 60, Playwright 4; 0 failed / 0 skipped / 0 xfailed /
  0 deselected
- **Command:** `./gate.ps1`
- **First run was RED, environmental — and self-referentially so:** the owner's manual-QA
  stack (`eoe-qa`, mid-walkthrough) held ports 5173/6379, and the gate's compose tests
  failed to bind (`Bind for 0.0.0.0:5173 failed: port is already allocated`) — exactly
  the D44 collision this repository documents. Remedy per D44: `qa-stack.ps1 down` (data
  volume kept, owner resumes with one `up`), full rerun GREEN.
- **Artifacts:** `.claude/rules/project-rules.json` 1.0.0 → 1.1.0 —
  `R1_record_keeping.verification_walkthrough`: every epic ships its own
  `guide/e{N}-verification.md` before its final gate, and amends prior walkthrough
  assertions it invalidates in the same batch that invalidates them. CLAUDE.md restates
  the rule. Rationale and mechanics in D45 (the qa-stack tooling tracks the product
  automatically; walkthrough prose does not — the F6/stale-handoff drift class, now
  closed by rule). PR #12 (the QA platform) was merged at the start of this batch.
- **Manual verification:** governance suite run standalone first (13 passed — the
  rules-JSON structure assertions are the risk point for this change); the E2 planning
  session in flight will be the rule's first live subject.

- **Tasks closed:** the owner-requested manual-QA platform (branch `qa-platform-1`;
  DECISIONS D44). Tooling + operator docs only; no production code or test changes.
- **Gate:** 29, GREEN
- **Tests:** backend 255, vitest 60, Playwright 4; 0 failed / 0 skipped / 0 xfailed /
  0 deselected (suite unchanged by design)
- **Command:** `./gate.ps1`
- **Artifacts:** `qa-stack.ps1` (repo root) — `up` builds and starts the documented
  compose stack under project `eoe-qa`, generates `deploy/.env` with fresh local secrets
  when missing (values never printed), seeds `app.seed --demo`, health-probes both ends,
  and prints the site URL + owner credentials with the boxed tear-down-before-gate
  warning (the recorded remedy for gate-15-class port collisions, D44); `down` keeps QA
  data, `reset` wipes it, `status` reports. `guide/e1-verification.md` — an 11-section
  checkbox walkthrough with expected results for every E1 feature (roll-up numbers, both
  themes, tree, tables, the full build-a-hierarchy CRUD walk with the conflict dialog's
  both paths, tag replace semantics, bulk import dry-run → gated partial accept with a
  paste-ready CSV, viewer/scoped-operator boundary probes) and a **shells audit**: one
  checkbox per deliberately-empty surface confirming it names its owning epic.
  `guide/README.md` TOC gains the walkthrough AND the previously missing
  `bulk-import.md` row, plus the one-command note.
- **Manual verification (the script proving itself):** fresh `up` from a clean volume —
  images built, all four containers healthy, demo seeded (2/6/6/28), credentials printed
  exactly once, health probes green; second `up` — idempotent "already seeded" path with
  no second credential print; `status` listed four healthy services; `down` removed
  containers, kept the volume, and freed the ports — after which this very gate ran
  GREEN, closing the loop on the warning the script prints.

- **Tasks closed:** E1.9, closing e1-batch-3 and with it **every E1 task** (E1.1-E1.9,
  gates 20-28; DECISIONS D43)
- **Gate:** 28, GREEN
- **Tests:** backend 255 passed (+2 in the new `test_seed_demo.py`), vitest 60,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `uv run python -m app.seed --demo` — one command, fully deterministic:
  "Earth Echoes Demo" with Redwood Coast (`redwood-coast`) and High Desert
  (`high-desert`), six named pods with aggregators `demo-agg-rc-01..03`/`hd-01..03`, 28
  listeners with locally-administered MACs, even-index GPS, first-listener pod tags.
  Fresh DB seeds owner + hierarchy with the password still printed exactly once;
  an existing owner gets hierarchy-only; an existing demo org refuses. The no-flag path
  is byte-identical to E0.12's (`test_seed.py` unchanged and green). The fixture is
  documented BY NAME in INTERFACES for E2/E6 and mirrored exactly by the frontend test
  fixture. `verify.py` gains an 11-step E1 hierarchy walk over real HTTP (one-call
  pod+aggregator, E1.4 reject/suffix pair, E1.7 tag replace, both D35 boundary checks,
  409-with-blockers, leaf-up teardown) with a children-first cleanup safety net.
  `guide/seed-script.md` documents the flag for operators.
- **Manual verification (including the walk carried from Gate 27):** an 11-check
  Chromium walk of the live dev stack (vite → uvicorn → seeded postgres) executed the
  E1.8 acceptance literally — logged in, verified the 28-listener hero and night theme,
  navigated the seeded tree, then **built a deployment → pod → listener hierarchy and
  ran a CSV import dry-run → partial-accept entirely in the UI**, exercising the
  conflict dialog's explicit suffix path live; zero `[data-status]` elements confirmed
  on the live inventory (D40). Screenshots retained in the session workspace. Seed
  determinism verified twice (subprocess suite + the walk stack).

- **Tasks closed:** E1.8 (+ the #16 Overview change), opening e1-batch-3 (DECISIONS
  D39-D42; project-changes #16; addendum PHASE1-4-04)
- **Gate:** 27, GREEN
- **Tests:** backend 253, vitest 60 passed (+16 across five new suites), Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** the nested `/inventory` surface on the fragment page shape — ContextBar
  (crumbs now real links, D41) over the 246px tree rail (36px rows, 14px/level indent,
  weight ladder, mono aggregator labels, CSS-drawn carets, route-tracking selection)
  beside four level pages. Tables run headless TanStack Table (D39, the epic's one new
  dependency) over the generalized `.data-table` vocabulary (D42; `.admin-table` retired,
  UsersAdmin repointed) with server-driven sort/pagination hitting the D7 wire grammar,
  mono identifier cells, and footer captions. First reusable `.form`/button vocabulary
  including the never-filled-red `.btn-danger`. The E1.4 conflict dialog consumes the
  {field, suggestion} detail and retries with auto_suffix only on the explicit click
  (suite-proven: never silent). The import screen runs dry-run-first with the partial
  commit structurally gated behind the row report + explicit checkbox; row outcomes are
  colored words, never device states. Overview is the V2·S1 roll-up with only E1-owned
  data (#16): hero "Listeners registered" from the D7 total, real deployment cards,
  honest EmptyStates naming E3/E5/E7. **D40's no-fabricated-status rule is
  gate-enforced**: tests assert zero `[data-status]` elements on every inventory route
  and the Overview. New tokens (danger-border + dark, indent-tree, space-px,
  duration-slow, width-treerail) additive per D21/D24.
- **Test changes (D42):** shell.test's route table +4 rows and the "/" heading row plus
  two auth.test assertions follow the recorded Overview retitle; nothing weakened, suite
  44 → 60.
- **Manual verification:** the five new vitest suites drive every flow end-to-end in
  jsdom against the real wire contracts (sort grammar captured off the requests, conflict
  dialog both paths, import dry-run → partial accept, viewer read-only); Playwright
  exercised the shell and both themes in real Chromium; the gate's compose-stack suite
  ran the live platform. **Not done at this gate:** a per-route browser walk of the new
  inventory surfaces — deliberately carried to Gate 28, where the E1.9 demo fixture
  seeds real data to walk against (the E1.8 acceptance "build a full hierarchy entirely
  in the UI" is verified there).

- **Tasks closed:** E1.7, closing e1-batch-2 (E1.6-E1.7, gates 25-26)
- **Gate:** 26, GREEN
- **Tests:** backend 253 passed (+5 in the new `test_tags.py`), vitest 44, Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `GET/PUT /{entity}/{id}/tags` on all five entities (listeners by MAC) —
  **PUT is wholesale replace, never merge**, storage normalized (trim/dedupe/sort) and
  validated (422 on >64 chars or control characters). Filter-by-tag proven on every
  entity list via GIN-backed containment. D35 scope rules carry over verbatim: viewers
  read, MANAGE_DEVICES writes, out-of-scope child items 404, org/deployment routes keep
  their 403 patterns. Tag writes audit as `<entity>.update {"changed": ["tags"]}`.
  E0_ROUTES += 10 (total E1 surface now 36 routes, matching the epic plan's spec-13
  parity list exactly). INTERFACES gains the tag storage model section E2's selection
  engine builds on.
- **Manual verification:** the tag → filter round trip walked on every entity level over
  the live API; replace semantics confirmed against the persisted rows.

- **Tasks closed:** E1.6, opening e1-batch-2 (DECISIONS D38)
- **Gate:** 25, GREEN
- **Tests:** backend 248 passed (+8 in the new `test_bulk_import.py`), vitest 44,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `POST /listeners/import` + `POST /aggregators/import` (JSON rows or raw
  CSV; `?partial=`/`?auto_suffix=` on the query string; 1000-row/1 MiB limits). Always
  200-with-report `{committed, created, failed, rows}` — row error codes reuse the D8
  strings as data, the wire vocabulary untouched. All-or-nothing default proven to roll
  back rows AND the request's audit record; partial accepts commit valid rows only;
  per-row SAVEPOINTs make in-file and DB duplicates one code path (flushed rows are
  visible to later rows' collision checks, so in-file auto-suffix ladders correctly).
  Scope enforced per row (cross-deployment rows are row-level `forbidden`). CSV formats
  normative in INTERFACES; `guide/bulk-import.md` gives operators worked examples.
- **Defect found and fixed during the task:** FastAPI parses `application/json` bodies
  before validating against a `bytes` parameter, 422ing JSON imports while CSV worked;
  fixed by reading the raw body via an async dependency and dispatching on content type
  in the endpoint.
- **Manual verification:** the mixed-file dry-run → partial-accept flow walked over the
  live API; CSV and JSON parity checked from one fixture including GPS floats and
  pipe-separated tags.

- **Tasks closed:** E1.5, closing e1-batch-1 (E1.1 through E1.5, gates 20-24;
  project-changes #15, addendum PHASE1-4-03, DECISIONS D37)
- **Gate:** 24, GREEN
- **Tests:** backend 240 passed (+10 in the new `test_identity_service.py`), vitest 44,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/inventory/identity.py` — services callable without MQTT, the exact
  seam E3.5 wires ("do not reimplement" recorded in INTERFACES with verbatim
  signatures). `handle_reported_identity` returns MATCHED / NAME_CONFLICT / MAC_CONFLICT
  / PROVISIONING_REQUIRED / UNKNOWN_MAC; conflicts quarantine the report and open a
  deduped `duplicate_identity` alert while the inventory row stays byte-identical
  (proven by full-column reload). `check_aggregator_membership` is a lookup, never
  sentinel equality; `require_known_aggregator` is the raising variant for ingest paths.
  Tables via migration `05c4858bfab5`: `quarantined_report` (append-only, deliberately
  no listener FK) and `inventory_alert` (open-alert dedupe via partial unique index
  `WHERE resolved_at IS NULL`, proven by raw insert; un-FK'd deployment scope). Alert
  types are data — the closed D8 wire vocabulary is untouched. System audit rows
  (`inventory.quarantine`, `inventory.alert`) with NULL actor. Services stage and never
  commit; the rollback probe proves an uncommitted session leaves nothing.
- **Manual verification:** migration round trip (`alembic check` clean at head,
  downgrade/upgrade clean); the full outcome table driven directly against a live
  ephemeral postgres; alert lifecycle (open → dedupe → resolve → fresh) walked
  end-to-end.

- **Tasks closed:** E1.4
- **Gate:** 23, GREEN
- **Tests:** backend 230 passed (+6 in the new `test_uniqueness.py`), vitest 44,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** spec 4.3 item 1 implemented literally. Name collision within a
  deployment rejects by default — `409 conflict` with `detail {"field": "name",
  "suggestion": "<name-2>"}`, the wire shape E1.8's conflict dialog consumes.
  `auto_suffix: true` (explicit body parameter, default false — the never-silent rule is
  itself a test) creates at the first free `name-N` and the audit row carries
  `{auto_suffixed, requested_name, final_name}`. MAC collision always rejects; no
  parameter overrides it. The compute/flush suffix race retries once with a recomputed
  name (proven by a monkeypatched stale-suffix simulation), then 409s.
  `next_free_name` joins `app/inventory/naming.py`; INTERFACES documents the parameter
  and the suggestion detail shape.
- **Manual verification:** the full suffix ladder (`sensor` → `sensor-2` → `sensor-3`)
  driven over the live API; cross-deployment name freedom re-proven.

- **Tasks closed:** E1.3
- **Gate:** 22, GREEN
- **Tests:** backend 224 passed (+4 in the new `test_pod_aggregator.py`), vitest 44,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `POST /pods` accepts an optional inline `aggregator` block — create-and-
  attach in one transaction with two audit rows and a single commit; the rollback proof
  shows a duplicate `aggregator_uuid` leaves neither the pod nor its audit row behind.
  `aggregator_uuid` platform-assigned (`uuid4().hex`) when omitted (spec 4.2); attach to
  an occupied pod stays 409 via `uq_aggregator_pod_id`. No new routes, tables, or plan
  changes — this is the spec-13 sentence implemented literally.
- **Manual verification:** both creation paths exercised over the API in the suite; the
  gate's compose stack ran the full platform live.

- **Tasks closed:** E1.2 (project-changes #13/#14; addenda PHASE1-4-01/02; DECISIONS
  D34, D35, D36)
- **Gate:** 21, GREEN
- **Tests:** backend 220 passed (+34: `test_hierarchy_crud.py` 12, `test_scoping.py` 22
  incl. parametrized visibility cases), vitest 44, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **First run was RED, one mypy error:** `app\api\aggregators.py:65: error: Function is
  missing a return type annotation [no-untyped-def]` — every test suite passed on that
  run. Fixed with an explicit `-> tuple[Aggregator, uuid.UUID]`; full rerun GREEN.
- **Artifacts:** 24 routes (written into the readiness lock from the OpenAPI dump):
  organizations without DELETE and with the single-org POST clamp (D34), full CRUD for
  deployments/pods/aggregators/listeners with listeners addressed by normalized MAC in
  every path. D7 envelopes with per-entity query models and filters (parent FKs, name
  icontains, tag containment, slug/mac/aggregator_uuid); child counts embedded in
  serializers for the E1.8 tree. Slug generation with collision suffixing and the D36
  freeze-at-first-pod rule. `app/scoping.py` ships the reusable visibility layer (D35):
  org-wide grants see all, scoped grants see their deployments, no-grant users get 403;
  deployment item routes keep the 403-before-lookup pattern while child items answer
  byte-identical 404s for out-of-scope and missing rows — the MAC-enumeration oracle
  defense, asserted by test. Deletes 409 with named blockers, including role assignments
  on deployments (D33). Every mutation audits `<entity>.<verb>` with deployment scope.
- **Manual verification:** the gate's compose-stack suite drove the live API end-to-end;
  scoping proven against five personas (owner, org viewer, scoped operator, scoped field
  tech, no-grants) across all four list surfaces and the item asymmetry.

- **Tasks closed:** E1.1, opening batch 1 of epic E1 (branch `e1-batch-1`)
- **Gate:** 20, GREEN
- **Tests:** backend 186 passed (+14: 12 in the new `test_hierarchy_schema.py` constraint
  suite, the users-admin 422 test, and the readiness seam test splitting into two), vitest
  44 passed, Playwright 4 passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** five singular-named tables (D30) — `organization`, `deployment`, `pod`,
  `aggregator`, `listener` — with every phase-doc fixed choice expressed as a database
  constraint: MAC as the listener primary key with a format CHECK (D31), one aggregator per
  pod via `uq_aggregator_pod_id`, listener names unique within their deployment via the
  set-once `deployment_id` stamp (D32), slug format CHECK + global unique, name-in-parent
  uniques, global `aggregator_uuid` unique, tags as GIN-indexed arrays and GPS columns
  front-loaded for E1.4-E1.7. Migrations `ee260dc1c1a8` (tables) and `53181716569c`
  (orphan-grant delete + role_assignment FK), both autogenerate-derived, hand-reviewed,
  with verified empty diff at head and a full downgrade/upgrade round trip. The E0.7 seam
  closed per D33: readiness seam test split into FK-present + audit-scope-never-FK halves,
  `test_rbac.py` fixture additively references real deployment rows (zero assertions
  changed), `/users` pre-validates assignment scopes (422), `verify.py` bootstraps and
  cleans a real `verify-dep-{tag}` deployment. `docs/INTERFACES.md` gains "Owned by E1:
  Entity schema" including the GPS-is-inventory-owned sentence E2 depends on.
- **Manual verification:** migration round trip run twice against an ephemeral postgres
  (`alembic check` reports no drift at head; `downgrade -2` then `upgrade head` clean); the
  gate's compose-stack suite ran the shipped verifier end-to-end over real HTTP, which
  exercised the new scoped-deployment bootstrap and cleanup live.

- **Tasks closed:** the records-and-test-hygiene batch a post-DES review of `23eff5d..f93f061`
  motivated (project-changes #11, #12; DECISIONS D28, D29; addenda PHASE0-4-05, PHASE0-4-06,
  DES-7-03). No product code changed.
- **Gate:** 19, GREEN
- **Tests:** backend 172 passed, vitest 44 passed (one added in `users-admin.test.tsx`),
  Playwright 4 passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **First run was RED, environmental:** 8 failed + 59 errors, every one
  `could not start ephemeral postgres: failed to connect to the docker API at
  npipe:////./pipe/dockerDesktopLinuxEngine … check if the daemon is running` — Docker
  Desktop was not running. Started Docker Desktop, engine reported up, full rerun GREEN with
  no test or code change in between.
- **Artifacts:** `phase-0-foundations.md` gains PHASE0-4-05 (D25's sidebar→top-bar
  replacement, previously recorded everywhere except the phase document) and PHASE0-4-06
  (correcting PHASE0-4-04's "acceptance criteria continue to hold unchanged" — the one-file
  token-sheet criterion stopped holding at the gate that appended it). project-changes #11
  gives Gate 17's backdrop change its own numbered entry and records the in-place amendment
  of #9; DES-7-03 mirrors it in the handoff doc. D28 records the fifth Gate 16 test change
  D26's inventory missed (theme-swap e2e rewrite, net +2 tests). Stale claims corrected in
  `DES.4-handoff.md` (`Screens.dc.html` is in the repo since Gate 16) and
  `DES-track-handoff.md` (working-document framing replaces the frozen-snapshot claim).
  `docs/frontend-guide.md` is subordinated to `docs/INTERFACES.md` "Frontend composition and
  shared components" and gains the owner's "Starting E1" brief (tokens via `tokens.ext.css`
  only; closed six-state `StatusChip` vocabulary; v1 mockups for layout, v2 for values, none
  visually QA'd; replace `EmptyState` panels, don't restyle around them; `ContextBar` is the
  breadcrumb home; `.admin-table` is the table pattern to generalize; TanStack Table not
  installed). `images/README.md` gains an explicit provenance/license TODO for
  `forest-background.jpg`, pending owner input.
- **Test changes (D29), both strengthenings, both verified red-then-green:** the
  `test_governance.py` baseline guard was pointed at a bogus path and failed loudly
  ("planning-baseline should pin at least 7 documents, got 0") before restoration; the new
  viewer-sees-the-Users-link test was pointed at a bogus link name and failed with the
  viewer's rendered DOM showing the real link present. The misleadingly named "hides the
  sidebar link from a viewer" test (vacuous on its viewer half since E0.9) is replaced by
  two honest assertions of D25's intent.
- **Manual verification:** red-then-green runs above, plus the governance suite (13 passed)
  and `users-admin.test.tsx` (6 passed) run standalone before the full gate.

- **Tasks closed:** DES.4 "three rules" item 3 (fonts vendored, never fetched) and the DES
  track's handoff into E1 (project-changes #10; addendum DES-7-02); D27 (font vendoring and
  the status-glyph subset)
- **Gate:** 18, GREEN
- **Tests:** backend 172 passed, vitest 43 passed (6 new in `fonts.test.ts`), Playwright 4
  passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `make gate`
- **Artifacts:** `frontend/public/fonts/` carries seven latin-subset woff2 files (~160 KB)
  declared by the new `frontend/src/styles/fonts.css`: IBM Plex Sans 400/500/600, IBM Plex
  Mono 400/600, Source Serif 4 600, and `eoe-status-glyphs.woff2`. Only weights the CSS
  actually uses are vendored. `frontend/tests/fonts.test.ts` gate-enforces the rule that made
  this a task at all — no `url(https:…)` or `@import` on any sheet, every `@font-face` src
  resolving to a committed file, every first-choice family in a `--eoe-font-family*` token
  supplied by a face, the glyph subset covering every status glyph token, and
  `.status-glyph::before` still naming the glyph family. `docs/INTERFACES.md` gains "Frontend
  composition and shared components": component contracts for `PageHeader`, `ContextBar`,
  `StatusChip`/`StatusLegend`, `EmptyState`, and `Can`, the two page-composition shapes, and
  the conventions E1.8 and E2 inherit. Each OFL license text ships beside the fonts.
- **The finding that shaped it (D27):** none of the six status glyphs — `●` U+25CF, `◐`
  U+25D0, `▲` U+25B2, `■` U+25A0, `✕` U+2715, `◆` U+25C6 — exists in IBM Plex Sans, IBM Plex
  Mono, or Source Serif 4, verified with fontTools against the **complete** families, not just
  these subsets. Vendoring the text faces alone would therefore have left the status
  vocabulary's shape channel to system fallback, and to tofu on a minimal air-gapped host
  (spec §15.1) — silently deleting one of the three channels status depends on. The shapes are
  now vendored too: Noto Sans Symbols 2 (OFL) subsetted to exactly those six codepoints, 568
  bytes, behind the additive token `--eoe-font-family-glyph`. This closes the Gate 16 caution
  about `◐` rendering as a hairline.
- **Manual verification:** the project owner ran `make gate` to completion (GREEN) and viewed
  the running compose stack in a browser, confirming the vendored typography renders as
  intended. **Not done at this gate:** a per-route screenshot pass in both themes, and any
  programmatic assertion that the glyph subset's shapes render distinctly at chip size — the
  gate checks the subset's declared coverage and wiring, not its rasterisation. Carry both
  into DES.8.
- **Known gaps:** the map engine is not built (E6 owns it). `Design System.dc.html` (the DES.1
  surface inventory and DES.5 component library) is not in this repository; the component
  contracts a session needs now live in `docs/INTERFACES.md` instead. DES.8's usability review
  stays blocked on E4–E6.

## 2026-07-30: Forest backdrop lands on every page (Gate 17 GREEN)

- **Tasks closed:** DES.7 asset follow-up — the image gap the Gate 16 entry recorded as a
  known deliberate gap is now closed (project-changes #9, amended)
- **Gate:** 17, GREEN
- **Tests:** backend 172 passed, vitest 37 passed, Playwright 4 passed; 0 failed / 0 skipped
  / 0 xfailed / 0 deselected
- **Command:** `make gate`
- **Artifacts:** `frontend/public/images/forest-background.jpg` is committed and now backs
  **every** page through `.shell-content`, not just the login hero. One additive token
  carries it: `--eoe-color-backdrop-scrim` in `tokens.ext.css` (0.93) with its night value in
  `tokens.ext.alt.css` (0.94), held a notch heavier because a daylight forest photo is far
  brighter than `--eoe-color-bg`. The scrim is near-opaque on purpose — the photo reads as
  texture, the effective background stays within a hair of `--eoe-color-bg`, and every
  contrast ratio the token sheets document still holds. `.login-page` keeps the much lighter
  `--eoe-color-overlay` and lets the photograph carry the screen, since its only content is
  an opaque card. Both treatments paint a `background-color` first, so a missing file
  degrades to a flat token color rather than to unreadable text. `public/images/README.md`
  documents the two treatments and the resize command.
- **Asset decision:** the file is committed web-sized — 1920×1280, ~925 KB, EXIF stripped —
  rather than as the 6000×4000 / 24 MB camera original supplied. It is fetched on every page
  now, not one, and git keeps binaries forever. `-strip` also drops the EXIF capture location
  the original carried.
- **Manual verification:** `make gate` was run to completion by the project owner and
  reported green. **Not done at this gate:** per-route screenshots of the new backdrop in
  both themes. The change is CSS-only and fails safe (flat token color) if the asset is
  missing, but the scrim values are a visual judgement that has not been eyeballed across all
  eight routes — carry it into DES.8's usability review.
- **Known gaps, unchanged from Gate 16:** the map engine is not built (E6 owns it). Fonts are
  not vendored, so IBM Plex and Source Serif 4 render in their fallback stacks, and the
  `sleeping` glyph (`◐`, U+25D0) still wants re-checking once they are.

## 2026-07-30: DES.7 applied — V2 shell, night theme, page skeletons (Gate 16 GREEN)

- **Tasks closed:** DES.7 for the shell and frame (project-changes #9; addendum DES-7-01);
  D24 (night theme ships, D21's dark-palette gap closed); D25 (shell restructure; primary nav
  lists every destination); D26 (four test fixes at this gate)
- **Gate:** 16, GREEN
- **Tests:** backend 172 passed, vitest 37 passed (10 in `tokens.test.ts`, including new
  checks 8-10), Playwright 4 passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `make gate`
- **Artifacts:** `Shell.tsx` is now V2·S1's dark top bar with horizontal nav, replacing the
  E0.4 sidebar (`shell-sidebar` → `shell-topbar`); `app.css` is rewritten against the v2
  direction, still literal-free. New shared components: `ContextBar`, `PageHeader`,
  `StatusChip`/`StatusLegend`, `EmptyState`, `ThemeToggle`. The night theme ships:
  `tokens.alt.css` stops being a fixture and the new `tokens.ext.alt.css` carries dark values
  for every extension color key, both scoped to `:root[data-theme="dark"]` so specificity —
  not import order — decides the theme; `lib/theme.ts` resolves a persisted manual override
  ahead of `prefers-color-scheme`. New keys in `tokens.ext.css` on D21's terms:
  `--eoe-color-action-contrast-muted`/`-action-raised`/`-accent-on-action`/`-brand-mark`,
  `--eoe-radius-pill`/`-round`, `--eoe-height-topbar`/`-contextbar`. Routes and v2-styled
  skeletons added for Map, Inventory, Configuration, and Provisioning, each naming the epic
  that fills it — no mock data. `docs/INTERFACES.md` documents the fourth sheet and the shell
  shape.
- **Manual verification:** all eight routes screenshotted at 1440×900 in Chromium in both
  themes; no horizontal overflow on any page; theme toggle, its persistence across reload,
  and the relit status palette confirmed against computed styles.
- **Known gaps, deliberate:** the map engine is not built (E6 owns it; Google Maps satellite
  online / operator-supplied local image offline per spec §15.1, ESRI later). Fonts are not
  vendored, so IBM Plex and Source Serif 4 render in their fallback stacks. The login
  backdrop expects `frontend/public/images/forest-background.jpg`, absent today, and degrades
  to a flat token color. **Caution:** the `sleeping` glyph (`◐`, U+25D0) rendered as a
  hairline mark in headless Chromium's fallback font — the status vocabulary depends on the
  glyph channel, so re-check it once the real fonts are vendored.

## 2026-07-30: DES.4 v2 tokens, DES-4-01 additive extension accepted (Gate 15 GREEN)

- **Tasks closed:** DES.4 (v2 "field notebook" value set, plus the DES-4-01 additive
  extension) (project-changes #8; addendum PHASE0-4-04); D21 (DES-4-01 accepted); D22 and
  D23 (test fixes at this gate)
- **Gate:** 15, GREEN
- **Tests:** backend 172 passed, vitest 33 passed (7 in `tokens.test.ts`, including new
  check 7), Playwright 2 passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `make gate`
- **Artifacts:** `frontend/src/styles/tokens.css` and `tokens.alt.css` take the DES.4 v2
  value set (warm paper neutrals, Okabe–Ito status colors, IBM Plex type, real night theme)
  — same 30 property names, same five namespaces, values only. `frontend/src/styles/
  tokens.ext.css` (new, accepted under D21/DES-4-01) adds the six-state status vocabulary
  with color/tint/glyph, plus `--eoe-border-width-*`, `--eoe-row-height-*`,
  `--eoe-control-height-*`, `--eoe-duration-*`, `--eoe-ease`; imported in `main.tsx`.
  `app.css`'s four `var(--eoe-space-1)` border/outline widths now use the new
  `--eoe-border-width-hairline: 1px`, fixing a 4px-instead-of-1px rendering defect. `docs/
  INTERFACES.md` "Design tokens" documents the extension; `project_planning/phase-0-
  foundations.md` gets addendum PHASE0-4-04. Two test fixes at this gate, both recorded:
  `frontend/e2e/theme-swap.spec.ts` no longer asserts on `fontFamily`/sidebar padding, which
  the real (not synthetic) night theme deliberately keeps identical to the light theme (D22);
  `backend/tests/test_governance.py`'s planning-doc immutability check now diffs only the
  documents that were actually part of `planning-baseline`, not every file currently in
  `project_planning/`, so new non-baseline material (the DES track's handoff docs, moved
  there and renamed at the project owner's direction: `project_planning/DES.4-handoff.md`,
  `project_planning/DES-track-handoff.md`) doesn't crash the check (D23). Known, explicitly
  deferred gap: `tokens.alt.css` does not yet carry dark-mode equivalents of the D21
  extension keys — real design work (per-pair contrast verification), not done in this batch.
- **Manual verification:** full local gate run twice — first run surfaced the two test fixes
  above plus an unrelated environmental port collision in `backend-tests`
  (`test_compose_stack.py`, `test_verify_tool.py`) against an already-running dev Compose
  stack on the same host ports; the project owner stopped that stack and reran themselves,
  confirming green, before this final `make gate` run was captured for the record above with
  no containers competing for ports.

## 2026-07-24: E0.12+ deployment verifier, USER guide, client-facing group (Gate 14 GREEN)

- **Tasks closed:** E0.12 extension (project-changes #7; addendum PHASE0-2-01)
- **Gate:** 14, GREEN
- **Tests:** backend 172 passed (the verifier gate test runs the shipped tool as a
  subprocess against a real compose stack: exit 0, every step PASS, temp accounts gone,
  TOTP secrets gone, audit rows surviving with nulled actors, no password material on
  stdout), vitest 32, Playwright 2; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `backend/app/verify.py` (`uv run python -m app.verify [--api URL]`):
  20-check owner-journey across health, auth, CSRF, the full TOTP lifecycle, user
  administration, RBAC deny paths for viewer and scoped operator, the audit trail, and
  live session revocation; guaranteed cleanup in a finally block; httpx promoted to main
  dependencies (D20). Top-level **`guide/`** client-facing group (layout change
  PHASE0-2-01): index README, getting-started, seed-script usage with implications,
  verify-deployment with implications; root README banner links it and demarcates docs/
  as engineering-internal.
- **Manual verification:** operator-style run against a fresh compose stack: 20/20 checks
  passed, "removed 3 temporary account(s); audit trail retained (immutable)", zero
  `verify-*` accounts left in the database. Clean teardown.

## 2026-07-24: E0-R readiness flight (Gate 13 GREEN)

- **Tasks closed:** E0-R (project-changes #6; addendum PHASE0-5-01) — the E0 exit-exam
  verifying E0.1 through E0.11 as a production-poised whole
- **Gate:** 13, GREEN
- **Tests:** backend 171 passed (14 new readiness tests), vitest 32, Playwright 2;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `backend/tests/test_e0_readiness.py`, organized by consumer epic as
  executable handoff documentation: locked surface contracts (exact route and table sets
  that later epics extend consciously; env-var documentation parity; OpenAPI operation
  completeness), E1 seams (un-FK'd nullable UUID scope columns awaiting E1.1's foreign
  key; MAC-wide audit entity ids; intact naming convention), E3/E4/E5 SecretStore consumer
  name shapes round-tripped, the E8.5 OIDC seam proven by minting a session for a user
  whose password can never verify, and production posture (data-seeded migration round
  trip to base and back; the prod nginx image proven to actually SERVE the built app, not
  just build; the API image proven non-root at UID 10001). Two defects found by designing
  the flight and fixed within it (D19): the compose frontend never received
  `VITE_API_BASE_URL` (the in-stack browser-to-API path had never worked), and the API
  container ran as root.
- **Manual verification:** through the real compose stack: `id -u` in the api container
  returned 10001; the frontend container carried `VITE_API_BASE_URL=http://localhost:8000`;
  health served 200 through the published port. Clean teardown.

Dated, reverse-chronological, append-only log of verified project state changes (rule R1,
`.claude/rules/project-rules.json`). An entry is written only after its gate passed with
0 failed, 0 skipped, 0 xfailed, 0 deselected, AND the task's manual verification steps ran.
Never in anticipation.

## 2026-07-24: Merge-blocking verification, protection confirmed absent

- **Event:** At the project owner's request, verified whether a failing `ci-green` actually
  blocks merging. Findings: `main` is unprotected (`protected: false`); the working
  account cannot change that (`admin: false`); a scratch draft PR (#4) with deliberately
  red checks was `MERGEABLE` with `mergeStateStatus: UNSTABLE`. Detection is fully
  functional; GitHub-side blocking is inactive until the repository owner applies the D17
  one-checkbox setting (require `ci-green` on `main`). Scratch PR closed and its branch
  deleted; the red run remains in Actions history as evidence. Until D17 is applied, merge
  discipline is procedural per rule R3.

## 2026-07-24: E0.12 Seed script (Gate 12 GREEN) — E0 build tasks complete

- **Tasks closed:** E0.12, closing batch 3 (E0.6 through E0.12) and with it every E0 build
  task
- **Gate:** 12, GREEN
- **Tests:** backend 157 passed (the seed acceptance test drives an empty unmigrated
  database through one command to migrated-and-seeded, parses the once-printed
  credentials, proves only the Argon2id hash is at rest, logs in as the org-wide owner
  over HTTP, and confirms re-seeding is refused), vitest 32, Playwright 2; 0 failed /
  0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/seed.py` (`uv run python -m app.seed`: migrates to head, creates the
  org-wide owner, prints credentials exactly once, audited as a system bootstrap, refuses
  re-seed); README dev setup updated (10 steps, at the budget)
- **Manual verification:** against the real compose stack: one seed command, the password
  printed exactly once, and the printed credentials logged in with HTTP 200. Clean
  teardown.
- **E0 definition of done (phase-0 section 5), swept:** compose-up to running API and
  frontend ✓ (lifecycle test at every gate); owner logs in with TOTP if enrolled, creates
  users, assigns roles ✓ (Gates 6/9/10); every mutation audits ✓; RBAC across all four
  roles with table-driven tests ✓ (Gate 7, test-critical suite); the secrets envelope
  round-trips and rotates under test ✓ (Gate 11); CI runs the full check suite ✓ (E0.5,
  every push); the shell renders entirely through the token sheet ✓ (discipline tests at
  every gate). Handoff artifacts: INTERFACES.md (every E0 section now concrete),
  DECISIONS.md (D1 to D18), the six-revision migration chain, the seed script,
  .env.example, and the token namespace delivered to the DES track at E0.4.

## 2026-07-24: E0.10 Optional TOTP (Gate 10 GREEN)

- **Tasks closed:** E0.10 (batch 3)
- **Gate:** 10, GREEN
- **Tests:** backend 156 passed (7 TOTP tests: full enrollment walk with wrong-code
  rejection, the acceptance triple at login — missing code 401 with totp_required, wrong
  code indistinguishable, right code 200 — unenrolled unaffected, secret only in
  SecretStore and never in responses post-enrollment, re-enroll and no-enrollment 409s,
  CSRF required, both mutations audited), vitest 32 (login page reveals and submits the
  code field), Playwright 2; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/api/totp.py` (enroll/confirm under /auth/totp); login TOTP gate in
  `app/api/auth.py`; `user.totp_enabled` via migration `0dd2c6d5b1d2` (hand-fixed: NOT NULL
  needs a server_default on populated tables — autogenerate review catching a real
  deploy-breaker); pyotp; login-page code field driven by the totp_required signal
- **Manual verification:** through the real compose stack: enrolled via curl (secret
  delivered once), confirmed with a computed code (200), login without a code returned the
  totp_required envelope, login with a live code returned 200. Clean teardown.

## 2026-07-24: E0.11 Platform secrets envelope encryption (Gate 11 GREEN, before E0.10)

- **Tasks closed:** E0.11 (batch 3; implemented before E0.10 per project-changes #5 /
  addendum PHASE0-4-03 so TOTP secrets ride SecretStore from birth)
- **Gate:** 11, GREEN
- **Tests:** backend 149 passed (8 secrets tests: KEK validation fail-loud, round trip and
  upsert, lifecycle, ciphertext-only at rest with fingerprint, isolated-database rotation
  with old-KEK rejection, tamper authentication failure, the acceptance log-grep, consumer
  docstring contract), vitest 31, Playwright 2; 0 failed / 0 skipped / 0 xfailed /
  0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/secrets.py::SecretStore` (AES-256-GCM envelope per spec 12.4, fresh
  DEK per write, KEK fingerprinting, `rotate_kek`), `secret` table migration
  `3f3b87c6623f`, store wired as `app.state.secret_store`, `EOE_KEK` validated fail-loud at
  startup (all test fixtures and the compose lifecycle env upgraded to structurally valid
  KEKs); INTERFACES SecretStore section
- **Manual verification:** scratch-database run: round trip OK, rotation re-wrapped and the
  new KEK read the value back, the old KEK was rejected with fingerprints only, and a
  DEBUG-level log capture contained no plaintext. Two silent-integrity catches during
  iteration, both fixed: a helper named `test_kek` was being collected as six phantom
  passing tests, and a blind rename briefly dropped the rotation test from collection.

## 2026-07-24: E0.9 User administration (Gate 9 GREEN)

- **Tasks closed:** E0.9 (batch 3)
- **Gate:** 9, GREEN
- **Tests:** backend 141 passed (10 new admin tests incl. the end-to-end acceptance flow:
  owner creates viewer over HTTP, viewer logs in, viewer 403 on administration; CSRF
  required on mutations; duplicate-email 409; unknown-role 422; audit row without secrets;
  deactivation kills the victim's live session mid-flight; password rotation; wholesale
  assignment replacement; self-lockout guards; D7 list with filters), vitest 31 (admin
  page, viewer denial, gated sidebar link, create flow, conflict surfacing), Playwright 2;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/api/users.py` (every E0 mechanism on one surface: RBAC gate + CSRF +
  audit + D7 + D1 revocation + self-lockout guard); `UsersAdmin` page behind `<Can>`, gated
  sidebar link, users client; test-infra hardening: the ephemeral-Postgres fixture moved to
  conftest with unique container names and Docker-assigned free ports after an orphaned
  fixed-port container broke iteration (docker_cli/docker_env centralized in conftest)
- **Manual verification:** through the real compose stack: owner created
  `new-viewer@example.com` via POST /users (201, viewer assignment echoed, no secrets);
  the viewer logged in (200), read their own identity, and got 403 on GET /users. The
  audit-row check for user.create is gate-tested (a display-only shell quoting slip cut
  the manual audit printout; the API behaved correctly throughout). Clean teardown.

## 2026-07-24: E0.8 Audit log (Gate 8 GREEN)

- **Tasks closed:** E0.8 (batch 3)
- **Gate:** 8, GREEN
- **Tests:** backend 131 passed (8 new audit tests: login/logout rows with request-id
  correlation, transaction atomicity via rollback, D3 revoke presence, no mutating audit
  routes, owner-gated read with the D7 envelope, action/actor/scope filters, newest-first
  default), vitest 26, Playwright 2; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** immutable `audit_log` table via hand-reviewed migration `c872acca01ec`
  with the reversible D3 `REVOKE UPDATE, DELETE`; `app/audit.py::record_audit` riding the
  caller's transaction; login/logout retrofitted (services now flush, endpoints commit once
  atomically with their audit row); `GET /audit` behind VIEW_AUDIT using the D7 contract;
  `script.py.mako` fixed to carry autogenerate imports (caught when the audit migration
  needed the JSONB dialect import); the `AuditQuery`-extends-`PageParams` pattern recorded
  as binding for all later list endpoints
- **Manual verification:** through the real compose stack: two logins and a logout produced
  exactly three audit rows; `?action=auth.logout` returned precisely the logout row with
  actor, entity, and request-id populated; default ordering newest-first; a revoked session
  querying /audit got 401 (the viewer-403 path is gate-tested). Clean teardown.

## 2026-07-24: E0.7 RBAC framework (Gate 7 GREEN)

- **Tasks closed:** E0.7 (batch 3)
- **Gate:** 7, GREEN
- **Tests:** backend 123 passed (37 new RBAC checks: 24-row table-driven permission
  matrix, no-assignment and union-of-assignments cases, all four roles against an
  org-level mutation and a deployment-scoped read over real HTTP with real sessions,
  scope isolation, invalid-scope 422, /me assignments), vitest 26 (can() mirror, Can
  component, cross-language parity), Playwright 2; 0 failed / 0 skipped / 0 xfailed /
  0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/auth/rbac.py` (Role, Permission, ROLE_PERMISSIONS, pure
  `has_permission`, `require_permission` factory with optional path-param scoping);
  `role_assignment` table via hand-reviewed migration `658a7e1ad594` (nullable un-FK'd
  scope per phase-0, NULL = org-wide); `/auth/me` returns assignments; frontend `can()`
  mirror + `<Can>`/`useCan` gating helper with a gate-enforced parity test against the
  Python source; INTERFACES RBAC section. This is the first of the four spec-14.5
  test-critical suites.
- **Manual verification:** through the real compose stack: seeded an owner with an
  org-wide assignment; login and `GET /auth/me` returned
  `assignments: [{role: owner, deployment_id: null}]`. One red gate during development:
  the vocabulary parity test's unanchored parse matched the module docstring and compared
  against an empty list; fixed with anchored bounds plus non-emptiness guards so an empty
  parse can never silently pass.

## 2026-07-24: E0.6 Local accounts and sessions (Gate 6 GREEN)

- **Tasks closed:** E0.6 (batch 3)
- **Gate:** 6, GREEN
- **Tests:** backend 86 passed (9 new auth tests incl. login/logout/expiry/wrong-password
  acceptance paths against a migrated ephemeral Postgres, plus signing/hashing primitives),
  vitest 19 (login page, session affordance), Playwright 2; 0 failed / 0 skipped /
  0 xfailed / 0 deselected; ruff, mypy strict (16 files), eslint, prettier, tsc clean
- **Command:** `./gate.ps1`
- **Artifacts:** `user` + `session` tables via hand-reviewed autogenerated migration
  `c07e17281417` (convention-named constraints, real downgrade; the E0.2 migration suite
  covers it automatically); Argon2id hashing; HMAC-signed opaque session cookie (D1);
  double-submit CSRF (D4); login/logout/me endpoints under the envelope contract with
  indistinguishable bad-credential 401s; `get_db`/`require_session`/`require_csrf`
  dependencies for E0.7+; token-styled login page, shell session affordance, MSW auth
  handlers; `EOE_SESSION_TTL_SECONDS` documented; INTERFACES auth section filled
- **Manual verification:** through the real compose stack with curl: login returned the
  user JSON and set both cookies; wrong password returned the 401 envelope; `/me` 200 with
  the session; logout without the CSRF header 403, with it 204; `/me` after logout 401
  (immediate revocation); the password string appeared nowhere in responses or the cookie
  jar. Stack torn down clean.

## 2026-07-24: E0.5 acceptance completed, CI green on main

- **Event:** PR #2 merged to `main` (merge commit `ea70563`); the push run `30118657466`
  on `main` completed with every job green. The phase-0 E0.5 acceptance criterion "CI green
  on main" is satisfied. "A failing test blocks merge" is mechanically proven by the
  red-path run (30117111859) and becomes enforced the moment the repository owner applies
  the one-time D17 setting (require the `ci-green` check on `main`).

## 2026-07-24: E0.5 CI pipeline (Gate 5 GREEN)

- **Tasks closed:** E0.5 (batch 2; PR #1 merged to main beforehand)
- **Gate:** 5, GREEN
- **Tests:** backend 77 passed (10 new CI-structural checks), vitest 14, Playwright 2;
  0 failed / 0 skipped / 0 xfailed / 0 deselected; all quality stages clean
- **Command:** `./gate.ps1`
- **Artifacts:** `gate.sh` refactored into the canonical stage registry (7 stages,
  per-stage invocation, `--list`, unknown-stage rejection); `.github/workflows/ci.yml`
  (7 jobs each invoking one registry stage, `ci-green` fan-in with fail-not-skip
  semantics, push+PR triggers, per-ref concurrency cancel, uv/npm/Playwright caching,
  Postgres service container for the literal D9 reversibility commands, BUILD_SHA from the
  commit SHA); `backend/tests/test_ci_pipeline.py` (two-way registry/workflow parity,
  fan-in completeness, checkout history/tags, migrations and containers coverage,
  extension-recipe documentation); INTERFACES "CI pipeline" section (3-step add-a-stage
  recipe); DECISIONS D15 (CI shape) and D16 (LF line-ending pin after the Gate 5 red run);
  `.gitattributes` with history renormalized
- **Manual verification:** registry proven by hand: `--list` prints all seven stages,
  unknown stage exits 2, `migrations-check` without DATABASE_URL exits 1, `backend-quality`
  correctly went red on an unformatted file during development. **Live-run verification
  complete:** first workflow run 30116966780 on `e0-batch-2` succeeded, all 8 jobs green in
  about 2.5 minutes wall clock, with the migrations job executing the literal D9
  reversibility commands against the service container (D9 closed). **Red-path proof:**
  scratch branch `ci-red-proof` with a deliberately failing test produced run 30117111859:
  `backend-tests` and `backend-quality` failed, unrelated stages stayed green, and
  `ci-green` ran and FAILED rather than skipping; run conclusion `failure`; branch deleted,
  run retained in Actions history as evidence. **Branch protection:** API attempt returned
  404 (admin required; account has WRITE); exact owner instructions recorded as D17.

## 2026-07-24: E0.4 React skeleton with neutral design tokens (Gate 4 GREEN)

- **Tasks closed:** E0.4 (closes the E0.0 to E0.4 batch)
- **Gate:** 4, GREEN
- **Tests:** backend 67 passed, frontend vitest 14 passed, Playwright 2 passed; 0 failed /
  0 skipped / 0 xfailed / 0 deselected across all stacks; ruff, mypy strict, eslint,
  prettier, tsc all clean
- **Command:** `./gate.ps1`
- **Artifacts:** routing (Overview, System, 404) inside a token-styled layout shell
  (sidebar plus content); TanStack Query provider with a health query; the binding token
  sheet `frontend/src/styles/tokens.css` (five namespaces, neutral theme) plus the
  test-only `tokens.alt.css` mirror; API client on `VITE_API_BASE_URL` (fail-loud, D2);
  MSW handlers so the frontend tests run with no backend; ESLint flat config with
  no-explicit-any as error plus Prettier; vitest suites (token discipline, shell/routing/
  query, api client); Playwright theme-swap suite; frontend prod image still builds
- **Manual verification:** through compose from outside the harness: the dev server served
  the shell HTML and the token sheet with `--eoe-*` definitions; the Playwright run proved
  in a real browser that loading the alternate value set changes computed background,
  color, font family, and spacing with zero code changes (E0.4's literal acceptance
  criterion). Clean teardown.

## 2026-07-24: E0.3 FastAPI skeleton (Gate 3 GREEN)

- **Tasks closed:** E0.3
- **Gate:** 3, GREEN
- **Tests:** 67 passed / 0 failed / 0 skipped / 0 xfailed / 0 deselected; ruff, mypy strict
  (9 source files), frontend typecheck clean. New suites: settings precedence (6), API
  skeleton (17), pagination contract (8); compose lifecycle strengthened to prove the
  versioned health endpoint, API-to-Postgres reachability, and the 404 envelope through the
  real stack
- **Command:** `./gate.ps1`
- **Artifacts:** `app/main.py::create_app` factory (uvicorn `--factory` mode);
  `app/settings.py` (env > TOML > default per D5, fail-loud, alias-named errors);
  `app/errors.py` (envelope as the only error shape, D8 vocabulary frozen, AppError rejects
  unknown codes at raise time); `app/middleware.py` (request-id middleware plus
  log-record-factory binding, security-header baseline); `app/api/health.py` (build/version/
  db-ping payload); `app/api/pagination.py` (D7 contract: PageParams, ListResponse,
  parse_sort, apply_page); CORS-with-credentials from `EOE_CORS_ORIGINS`; Dockerfile
  BUILD_SHA arg and versioned healthcheck; DECISIONS D14
- **Manual verification:** through compose from outside the harness: health returned
  `build_sha` injected via the BUILD_SHA build arg and `database: ok`; unknown path returned
  the exact envelope with `not_found`; inbound `X-Request-ID` echoed with security headers
  present; CORS returned allow-credentials, the configured origin, and exposed X-Request-ID.
  Clean teardown.

## 2026-07-23: E0.2 Postgres and migrations (Gate 2 GREEN)

- **Tasks closed:** E0.2
- **Gate:** 2, GREEN
- **Tests:** 36 passed / 0 failed / 0 skipped / 0 xfailed / 0 deselected, zero warnings
  (migration suite: chain integrity, non-trivial-downgrade AST check, filename template,
  conventions doc, plus upgrade/downgrade/round-trip/empty-autogenerate-diff/constraint
  naming against a real ephemeral Postgres); ruff, mypy strict, frontend typecheck clean
- **Command:** `./gate.ps1`
- **Artifacts:** `backend/app/db.py` (binding MetaData naming convention plus `Base`);
  `backend/alembic.ini` (no URL stored; `path_separator = os`); `backend/alembic/env.py`
  (targets `app.db.Base`, URL exclusively from `DATABASE_URL`); reversibility-annotated
  `script.py.mako`; baseline root migration `4a07fe3a8e54` (deliberately empty, the single
  downgrade exemption); `docs/migration-conventions.md`; Gate 2 suite
  `backend/tests/test_migrations.py`; DECISIONS D13 (ephemeral Postgres via direct docker
  run, amending D6)
- **Manual verification:** by hand against a scratch Postgres container: `upgrade head`,
  `downgrade -1`, `upgrade head`, `downgrade base`, `upgrade head` all succeeded and
  `alembic current` reported `4a07fe3a8e54 (head)`; container removed cleanly.

## 2026-07-23: E0.1 Repository and container scaffolding (Gate 1 GREEN)

- **Tasks closed:** E0.1
- **Gate:** 1, GREEN
- **Tests:** 27 passed / 0 failed / 0 skipped / 0 xfailed / 0 deselected (integration
  included: compose lifecycle up, HTTP probes, pg_isready, clean teardown, prod image
  build); ruff check, ruff format, mypy strict, and frontend `tsc --noEmit` all clean
- **Command:** `./gate.ps1`
- **Artifacts:** fixed monorepo layout (`backend/app` typed ASGI placeholder, `frontend/`
  minimal Vite React TS app, `deploy/`, `sim/`, `docs/`); `backend/Dockerfile` (Python 3.12
  plus uv); `frontend/Dockerfile` (multi-stage: dev Vite server, prod nginx static build per
  D2); `deploy/docker-compose.yml` (api, frontend, postgres, redis on ports 8000/5173/5432/
  6379, healthchecks, fail-fast env interpolation); `deploy/.env.example` (names only);
  `README.md` (dev setup in 9 steps); Gate 1 suites `test_repo_layout.py` and
  `test_compose_stack.py`; `gate_runner.py` refactored with fail-closed `enforce()`
- **Manual verification:** from outside the test harness: `docker compose up -d --wait`
  brought all four services healthy; `curl` returned the API placeholder JSON on 8000 and
  HTTP 200 on 5173; `down -v` removed everything. Compose fail-fast interpolation observed
  firing on a deliberately missing `EOE_KEK`. Two red-gate infra fixes recorded (D12 docker
  credential-helper PATH; first run's failure output preserved in the session log).

## 2026-07-23: E0.0 Governance scaffolding (Gate 0 GREEN)

- **Tasks closed:** E0.0 (task added by project-changes #1)
- **Gate:** 0, GREEN
- **Tests:** 15 passed / 0 failed / 0 skipped / 0 xfailed / 0 deselected; ruff check and
  ruff format both clean
- **Command:** `./gate.ps1`
- **Artifacts:** `.claude/rules/project-rules.json` (rules R0 to R3); `CLAUDE.md` rules
  loader; `docs/DECISIONS.md` (D1 to D11); `docs/project-changes.md` (#1 to #4);
  `docs/INTERFACES.md` skeleton; four addenda across `project_planning/`; `.gitmessage`
  wired as `commit.template`; `gate.ps1`, `gate.sh`, `Makefile`; backend test scaffold
  (governance suite, git-hygiene suite, `tests/gate_runner.py`)
- **Manual verification:** R0 guard proven end to end: a deliberately skipped test makes
  `tests/gate_runner.py` exit 1, and removing it returns exit 0 (this check exposed and
  fixed the pytest 9 exitstatus defect recorded as D11). Planning documents verified
  byte-identical to the `planning-baseline` tag after addendum stripping.
