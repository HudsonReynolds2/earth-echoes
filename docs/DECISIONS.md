# Decisions

Deviations from the spec or a phase document, and implementation choices the documents left
open, with rationale (implementation-handbook.md section 1, rule R1). Feed these back into
the next spec or phase-doc revision. Newest first within each batch.

## D51 (2026-08-04): Config secrets — the marker, the config: namespace, and the commit-ordering rules

- **Decision:** a secret-flagged override key never stores plaintext. The row holds the
  marker `{"$secret": "config:{entity_type}:{entity_id}:{key}"}` and the plaintext lives
  in SecretStore under that name — the new `config:` namespace beside
  `totp:`/`deployment:`/`bundle:` (flagged additive edit to the E0-owned SecretStore
  contract; the readiness round-trip test covers the new shapes, including the MAC-keyed
  listener form whose name contains colons). Wire semantics: a redacted read renders a
  set secret as the keep sentinel `{"$secret_set": true}`; PUT accepts a plaintext
  string (set/replace), the sentinel (keep — 422 if nothing is stored), or omission
  (unset). Commit ordering: SecretStore commits through its own sessions, so plaintext
  puts land immediately (an aborted caller transaction strands an unreachable secret,
  harmless — its marker never landed) and deletions are returned to the caller as
  `secret_names_to_delete` to run AFTER its commit, because deleting first would lose
  the value on rollback.
- **Reference:** spec 5.3, 12.4; phase-2 E2.2; app/config/overrides.py;
  test_entity_overrides.py.

## D50 (2026-08-04): The override level rule — at-or-above lowest level (spec over phase doc)

- **Decision:** `validate_override_map` enforces spec 5.3's direction: a key may be
  overridden at its lowest level or at any ancestor level, never below. The phase doc's
  "below is permitted" sentence loses (project-changes #17, addendum PHASE2-4-01).
  `lowest_level='any'` behaves as listener — settable everywhere. Errors are returned
  per key, all at once, sorted, each naming the key (folded into one D8
  `validation_error` 422 by the API layer in E2.4). Additional validator rules the
  documents left open: null is never a value (remove the key to unset), `object` values
  are capped at 2 KiB (opaque is not unbounded; capture.schedule's internal schema is
  firmware/E4 territory), int values reject booleans (Python bool-is-int trap).
- **Reference:** spec 5.3, 5.1; phase-2 E2.2; owner decision 2026-08-04;
  app/config/validation.py.

## D49 (2026-08-04): Inventory resolution extends to identity.name and identity.mac

- **Decision:** the catalog marks four keys `resolution='inventory'`: `location.gps_lat`
  and `location.gps_lon` (mandated by E1's INTERFACES contract) plus `identity.name` and
  `identity.mac`. All four read from listener columns (D31/D32 own those fields) and
  reject override writes with a 422 naming the key and pointing at
  `PATCH /listeners/{mac}` (arrives with E2.2's validator).
- **Rationale:** identity.* has exactly the character the E1 contract fixed for
  location.*: the listener row is the source of truth (MAC is the immutable key, name is
  DB-unique per deployment). A config override shadowing either would fork the identity
  model E1.5's services depend on.
- **Reference:** spec 5.3, 4.2; E1 INTERFACES "hierarchy schema"; phase-2 E2.2.

## D48 (2026-08-04): Service-key write block — the E5 stub (owner-directed)

- **Decision:** all eight `telemetry.*` keys plus `upload.s3_bucket`, `upload.s3_endpoint`,
  `upload.s3_access_key`, `upload.s3_secret_key` carry
  `write_restricted='service_onboarding'`: their catalog rows exist and their defaults
  merge into effective config (spec 16.4 needs that), but the generic override PUT
  rejects them with a message naming E5's onboarding flow — a documented R2 stub, not a
  missing feature. **`upload.s3_prefix` is deliberately outside the block** (owner ruling
  2026-08-04): spec 5.1 names it an aggregator-level setting the operator sets, while
  spec 16's flow writes the deployment-level endpoints/credentials.
- **Rationale:** spec 5.3's closing paragraph — "the deployment services onboarding flow
  writes them rather than the operator editing them key by key" — plus E2's out-of-scope
  list deferring that flow to E5. Blocking now avoids two writers when E5 lands.
- **Reference:** spec 5.3, 5.1, 16; phase-2 "Out of scope"; owner decisions 2026-08-04.

## D47 (2026-08-04): Catalog storage — singular table, in-migration convergent seed, schema-document endpoint

- **Decision:** the spec-5.3 catalog is a `settings_catalog` table (singular per D30)
  seeded in-migration from the `app/config/catalog.py::CATALOG` constant — the single
  source, gate-pinned against a hardcoded spec key list AND against the seeded rows
  field for field. `seed_catalog()` is an upsert-plus-prune, so replays converge on the
  current constant (neutralizing the import-app-code-in-a-migration hazard); catalog
  evolution = constant edit + sync migration + `CATALOG_VERSION` bump in one batch.
  `GET /config/catalog` returns `{version, items}` sorted by key — a schema document,
  deliberately NOT a D7 list envelope (it is rendered wholesale, never paginated).
  `lowest_level='any'` (logging.verbosity) behaves as `listener` for the level rule.
  The DB column for the default is `default_value` (SQL keyword avoidance); the wire
  field stays `default`. Bounds added beyond the spec table: confidence_threshold 0-1
  (definitionally), noted in the row.
- **Reference:** spec 5.3; phase-2 E2.1 and "Catalog storage"; test_settings_catalog.py.

## D46 (2026-08-04): Gate-30 commit message reworded — one-time deviation from R3's never-amend clause (owner-approved)

- **What happened:** the original gate-30 commit message named the repository's
  instructions file by filename. That filename contains an R3 forbidden substring, and
  `test_git_hygiene.py` scans every commit message in history case-insensitively — so CI
  on PR #13 went red (`backend-tests` → `ci-green`), and every future gate and CI run on
  any branch containing that commit would fail forever. A follow-up commit cannot remove
  a string from history.
- **Decision:** reword the commit message only (tree byte-identical; the gate-30 content
  and its green result are untouched), move the `gate-30` tag to the reworded commit, and
  force-push branch + tag. This deviates, once, from R3's "never amend or force-push a
  tagged gate commit" — the two R3 clauses were in genuine conflict, and the owner chose
  clean history over tag immutability (the alternative was whitelisting the filename in
  the scanner, weakening the bright line). Owner-approved 2026-08-04.
- **Why the gate could not catch it:** the gate runs BEFORE the commit exists; a commit
  message defect is only ever caught by CI or the NEXT gate, after the push. This is a
  structural blind spot of the gate-then-commit sequence, not a broken test.
- **Prevention (binding on future sessions):** never write the instructions file's
  filename — or any other R3 forbidden substring — in git-visible text: commit messages,
  PR titles/bodies, issues, tags, release notes. Refer to it as "the project instructions
  file". The PR #13 body was edited to comply in the same batch.
- **Reference:** rule R3 (attribution + push protocol); `backend/tests/test_git_hygiene.py`;
  PR #13 CI runs of 2026-08-04.

## D45 (2026-08-04): Rules 1.1.0 — walkthrough currency joins R1 (owner-directed)

- **Decision:** `.claude/rules/project-rules.json` gains `R1_record_keeping.verification_walkthrough`
  (version 1.0.0 → 1.1.0; CLAUDE.md restates it): every epic ships its own
  `guide/e{N}-verification.md` (indexed in guide/README.md) before its final gate, and an
  epic that invalidates a prior walkthrough's assertions amends them **in the same batch
  that invalidates them** — the clause that catches E3 flipping E1's no-status
  expectations. Walkthrough changes ride gated batches like everything else.
- **Why a rule and not convention:** the split is mechanical vs prose. `qa-stack.ps1`
  tracks the product automatically (current images, current migrations, the test-pinned
  demo fixture); the walkthrough is prose that nothing compels forward — exactly the
  drift class already observed twice (frontend-guide outside the record loop, hygiene F6;
  the stale DES handoff statements). Owner directed the amendment 2026-08-04 before E2
  planning so E2 becomes the rule's first subject.
- **Governance mechanics:** the new key sits BESIDE `logs`/`addendum_convention`
  (test_governance pins `logs` as an exact set); rules-JSON structure re-validated by the
  gate.
- **Reference:** rule R1; guide/e1-verification.md; DECISIONS D44; hygiene finding F6
  (project-updates 2026-08-01).

## D44 (2026-08-02): qa-stack.ps1 — the manual-QA stack and the ports rule

- **Decision:** `qa-stack.ps1` (repo root) is the one-command manual-QA entry point:
  `up` builds and starts the documented compose stack under the dedicated project name
  **`eoe-qa`**, generates `deploy/.env` with fresh local secrets when missing (never
  printing values; `.env` is gitignored), seeds `app.seed --demo` host-side, health-
  probes both ends, and prints the site URL + owner credentials. The seed's refusal on
  re-run is treated as the idempotent "already seeded" path, not a failure. `down`
  keeps the `eoe-qa_postgres_data` volume (QA data survives restarts); `reset` wipes
  it; `status` reports. `guide/e1-verification.md` is the walkthrough it exists for.
- **The ports rule, recorded as the fix for the gate-15-class incident:** the gate's
  compose suites (`eoe-gate-test`, `eoe-verify-test`) bind the SAME host ports
  (8000/5173/5432/6379), so a running QA stack reds the gate. The script prints a boxed
  tear-down-before-gate warning on every `up`, and the walkthrough repeats it twice.
  Distinct project names isolate containers/volumes, not ports — the warning is the
  remedy, not a workaround.
- **Scope note:** tooling + operator documentation only; no production code, no test
  changes, no planning-document impact (hence no project-changes entry). Delivered
  through the full rhythm (branch, green gate, PR) — the frontend-guide record-skipping
  mistake (hygiene finding F6) deliberately not repeated. `guide/README.md`'s TOC also
  gains its missing `bulk-import.md` row.
- **Reference:** guide/e1-verification.md; guide/getting-started.md;
  backend/tests/test_compose_stack.py; project-updates 2026-07-30 (the gate-15
  collision); D43.

## D43 (2026-08-02): The demo fixture — seed --demo semantics and the verifier's E1 walk

- **Decision:** `uv run python -m app.seed --demo` seeds the canonical hierarchy in the
  same one command (fresh DB: owner + hierarchy, password still printed exactly once;
  existing owner: hierarchy only, nothing re-printed; existing demo org: refuse, exit 1).
  The **no-flag path is byte-identical to E0.12's** — `test_seed.py` runs unchanged. The
  fixture is fully deterministic (no randomness): org "Earth Echoes Demo"; Redwood Coast
  (`redwood-coast`) and High Desert (`high-desert`); three named pods each; aggregators
  `demo-agg-rc-01..03`/`demo-agg-hd-01..03`; 28 listeners at 8/5/3 + 6/4/2 with
  locally-administered MACs (`02:EE:0E:…`), even-index GPS, first-listener pod tags —
  documented by name in INTERFACES so E2/E6 reference rows without re-deriving them, and
  mirrored exactly by `frontend/tests/inventory-fixture.ts`. One system audit row
  (`inventory.seed_demo`) summarizes counts.
- **Verifier:** `verify.py` gains an 11-step E1 walk over real HTTP (create deployment →
  pod-with-aggregator in one call → listener by MAC → E1.4 reject/suffix pair → E1.7 tag
  replace → D35 scoped-visibility and 404-oracle checks → 409-with-blockers → leaf-up
  teardown); cleanup's safety net now removes hierarchy rows under any `verify-%`
  deployment children-first, so a run that dies mid-walk still leaves nothing.
- **Reference:** phase-1 E1.9; DECISIONS D20 precedent (verifier DB-side bootstrap);
  backend/tests/test_seed_demo.py; guide/seed-script.md.

## D42 (2026-08-02): .admin-table generalizes to .data-table; gate-27 test retitles

- **Decision:** `.admin-table` (E0.9, UsersAdmin-only) becomes the shared **`.data-table`**
  vocabulary with the v2 header treatment (raised mono uppercase band) — one table
  language for E1.8's four tables and everything later (INTERFACES: "no second
  vocabulary"); UsersAdmin repointed in the same commit, its `users-table` testid and
  suite untouched. E1.8 also ships the first reusable `.form`/button vocabulary
  (`.auth-form` stays login-specific), including `.btn-danger` as surface-fill +
  alerting ink + tinted border — never a filled red button.
- **Test changes at this gate (R0):** `shell.test.tsx`'s route table gains four inventory
  rows and its `/` heading row follows the Overview retitle to "Organization overview"
  (project-changes #16); `auth.test.tsx`'s two post-login assertions follow the same
  retitle. Both are consequences of a recorded plan change, not weakenings; every other
  assertion is untouched and the suite grew 44 → 60.
- **Reference:** INTERFACES "Frontend composition"; project-changes #16; DECISIONS D25.

## D41 (2026-08-02): ContextBar crumbs become real links

- **Decision:** `Crumb.to` now renders a router `<Link>` (the DES.7 component rendered
  `to` as a styled span — declared but never wired); the final crumb carries
  `aria-current="page"`. Additive change to a DES-owned component, flagged here per
  INTERFACES' rule: E1.8 is the ContextBar's first real consumer and the breadcrumb is
  its reason to exist (D25). `to`-less usage (the Map page) is unaffected. Noted for
  DES.8's review.
- **Reference:** INTERFACES "Frontend composition" (ContextBar contract); D25.

## D40 (2026-08-02): No fabricated status anywhere in E1's UI

- **Decision (owner-directed, 2026-08-02):** E1.8 builds the tree, tables, and detail
  surfaces to the design geometry with status slots designed in, but renders **no device
  state anywhere** — no StatusChip rows, no rollup dots, no distribution bars, no
  "devices online" hero — because no reported state exists until E3 wires MQTT, and the
  project rule is no mock data, ever. The honesty is gate-enforced:
  `inventory-tree.test.tsx` and `overview.test.tsx` assert **zero `[data-status]`
  elements** on every inventory route and the Overview. Where the mockups draw status,
  E1 shows structure/counts/identity and names the owning epic in visible copy ("Device
  status arrives with E3 · services with E5"). E3 removes the guard deliberately when
  real state lands. Flagged for DES.8's review (the mockups draw dots the product
  intentionally omits).
- **Reference:** epic plan owner decisions; S7's own copy ("Postgres-owned data stays
  live"); DECISIONS D25.

## D39 (2026-08-02): @tanstack/react-table — E1's one new frontend dependency

- **Decision:** `@tanstack/react-table` ^8.21 (the phase doc's fixed choice for E1.8's
  device tables; the frontend guide records installation as an explicit decision, made
  here). Used strictly headless: all rendering through the shared `.data-table` classes;
  `manualSorting`/`manualPagination` because the D7 envelope makes the server the source
  of truth — the page serializes TanStack state to the wire grammar
  (`sort=name|-name`, `limit`/`offset`). No form, validation, or CSV libraries were
  added; the server is the parser and validator (D38).
- **Reference:** phase-1 E1.8; docs/frontend-guide.md "Starting E1"; D7.

## D38 (2026-08-02): Bulk import — 200-with-report, all-or-nothing default, savepoint rows

- **Decision:** `POST /listeners/import` and `POST /aggregators/import` accept
  `application/json` (`{"rows": [...]}`) or raw `text/csv`; options ride the query string
  (`?partial=`, `?auto_suffix=` — listeners only) because a CSV body cannot carry them.
  Limits: 1000 rows, 1 MiB. A well-formed request always answers **200 with a job
  report** `{committed, created, failed, rows: [{row, status, entity_id, name, error}]}`
  — row results are data, not an error envelope, and row `error.code` strings reuse the
  D8 vocabulary as data without extending the wire codes. All-or-nothing is the default:
  any failed row rolls back every row AND the audit record (suite-proven), and the
  committed=false report doubles as the dry run the E1.8 UI shows before an explicit
  partial accept. Rows execute under per-row SAVEPOINTs so constraint violations become
  row errors, and flushed rows are visible to later rows' collision checks — in-file and
  DB duplicates share one code path. Scope is enforced **per row** (cross-scope rows are
  row-level `forbidden`), so the endpoints need only session + CSRF. Audit: one
  `<entity>.import` row per request with counts, flags, and created ids.
- **CSV documentation split:** the column format is normative in `docs/INTERFACES.md`
  (phase-doc requirement); `guide/bulk-import.md` shows operator examples and defers to
  it (PHASE0-2-01 routes operator material to /guide) — both, no conflict.
- **Reference:** phase-1 E1.6; spec 13; DECISIONS D8, D35; backend/tests/test_bulk_import.py.

## D37 (2026-08-02): Report-time identity — return-based services, append-only quarantine, deduped alerts

- **Decision:** E1.5 ships as `app/inventory/identity.py` — services plus two tables, no
  HTTP surface, no UI (E3.5 wires MQTT and must not reimplement the logic). The API is
  **return-based**: `handle_reported_identity(db, ReportedIdentity) ->
  IdentityResolution{outcome, listener, quarantined, alert}` with outcomes
  MATCHED / NAME_CONFLICT / MAC_CONFLICT / PROVISIONING_REQUIRED / UNKNOWN_MAC — friendlier
  for E3's consumer loop than exception control flow; `require_known_aggregator` provides
  the raising variant (`ProvisioningRequiredError`) for ingest paths.
- **Semantics fixed here:** conflicts NEVER touch inventory rows (suite proves
  byte-identical reload); `quarantined_report` **appends** — every conflicting report is
  evidence — and carries **no FK to listener** (must survive deletion and describe devices
  inventory never held); `inventory_alert` **dedupes on the open alert** per
  (alert_type, entity_type, entity_key) via a partial unique index
  (`WHERE resolved_at IS NULL`), app-checked first so a repeat conflict returns the
  existing alert; a resolved alert permits a fresh one. `alert.deployment_id` is scope
  for filtering, deliberately un-FK'd (same reasoning as audit scope, D33).
  `duplicate_identity`/`provisioning_required` are **alert types, not wire error codes** —
  the closed D8 vocabulary is not extended. Services stage rows and never commit; audit
  rows (`inventory.quarantine`, `inventory.alert`) are system-originated (actor NULL).
- **Reference:** spec 4.3 items 2-3, spec 17 item 9; phase-1 E1.5; project-changes #15
  (PHASE1-4-03); migration `05c4858bfab5`; backend/tests/test_identity_service.py.

## D36 (2026-08-02): The deployment slug freezes at the first pod

- **Decision:** the concrete rule behind the phase doc's "editable before first use":
  `slug` may be set at create (else generated: NFKD-strip to ASCII, lowercase, squash
  non-alphanumerics to hyphens, trim, cap 63, collision suffix `-2`, `-3`, …) and changed
  via PATCH **only while the deployment has zero pods**; afterwards a differing slug is
  409 `conflict`. "First use" means "first child pod" because the `{dep}` MQTT namespace
  (spec 7.2) only matters once devices can exist under it. E3 may tighten this rule
  (e.g. freeze permanently once a broker is live), never loosen it. Known edge, accepted:
  a deployment that had pods, deleted them all, may change its slug again pre-E3 —
  recorded in INTERFACES so E3 re-examines it.
- **Reference:** phase-1 §2; spec 7.2; test_hierarchy_crud.py slug lifecycle tests.

## D35 (2026-08-02): Scoped visibility — the filter, the permission map, and the 403/404 asymmetry

- **Decision:** `app/scoping.py` is the single source for result-set visibility:
  `visible_deployments(assignments, permission)` -> `"all" | set[ids]`,
  `scope_filter(...)` for lists (deployments on id, pods on deployment_id, aggregators
  via the pod join, listeners on the D32 stamp), `require_any_assignment` for surfaces
  every role may read. Permission map: reads = VIEW_STATUS (org reads = any assignment);
  child writes = MANAGE_DEVICES in the target deployment + CSRF; org writes and
  POST /deployments = org-level MANAGE_DEVICES. **No change to rbac.py anywhere in E1** —
  the locked matrix and the frontend parity test are untouched; this suite lives in the
  new `test_scoping.py`, not in the test-critical file.
- **The asymmetry:** `/deployments/{deployment_id}` routes keep E0.7's 403-before-lookup
  pattern (safe: the check precedes any existence lookup). Child items answer **404 for
  out-of-scope and missing alike** — MACs are enumerable (OUI + counter), so a
  403-on-existing would be an existence oracle; the suite asserts the two 404 bodies are
  byte-identical. POSTs answer 403: the client supplied the parent scope, denial confirms
  nothing.
- **Reference:** spec 12.3, 13; DECISIONS D32; backend/tests/test_scoping.py.

## D34 (2026-08-02): Organization surface — no DELETE, single-org POST clamp

- **Decision:** spec 13 lists no DELETE for `/organizations` and wins over E1.2's "all
  five entities" wording (owner-confirmed 2026-08-02; project-changes #13, addendum
  PHASE1-4-01). POST /organizations 409s while an organization exists (spec 12.1
  single-org v1; project-changes #14, PHASE1-4-02). Cross-reference D32: the clamp is
  what makes global `aggregator_uuid` uniqueness equal the spec's within-org rule; a
  future multi-org change relaxes both together. Org reads are gated by
  `require_any_assignment` (a deployment-scoped operator still needs the org name for
  the tree); org writes need org-level MANAGE_DEVICES.
- **Reference:** spec 13, 12.1; phase-1 §4 E1.2; DECISIONS D32.

## D33 (2026-08-02): E1.1 flips the role_assignment FK; audit scope is never one

- **Decision:** `role_assignment.deployment_id` gains its real foreign key (the seam phase-0
  E0.7 fixed explicitly), plain NO ACTION; `audit_log.scope` is **deliberately never FK'd**,
  permanently — D3 immutability means audit rows outlive the deployments they reference.
- **Consequences, recorded before the gate (rule R0):** (1) readiness test
  `test_scope_columns_are_uuid_nullable_and_not_yet_foreign_keys` is replaced by two tests —
  the role_assignment half inverted as the test was designed to be, the audit half made
  permanent with a test asserting NO FK ever appears. (2) `test_rbac.py` (test-critical)
  receives an **additive fixture change only**: the module fixture inserts an organization
  and real deployment rows for DEPLOYMENT_A/B because scoped grants now reference real rows;
  no assertion or matrix row changed. (3) `test_users_admin.py` likewise creates a real
  deployment; a new test pins the 422. (4) `/users` assignment bodies now pre-validate
  deployment existence (422 `validation_error`) so an FK violation is never miscaught by the
  email-conflict IntegrityError handler. (5) Migration `53181716569c` DELETES orphan scoped
  grants rather than NULLing them — NULL means org-wide, so NULLing would silently escalate
  a scoped grant; deleted orphans referenced deployments that never existed and are accepted
  as unrestorable. (6) `verify.py` bootstraps a real `verify-dep-{tag}` deployment (and org
  if none exists) for its scoped-operator step and removes both in cleanup.
- **Reference:** phase-0 E0.7; phase-1 E1.1; docs/INTERFACES.md role_assignment section;
  DECISIONS D3.

## D32 (2026-08-02): Listener carries a set-once deployment_id stamp; aggregator_uuid unique globally

- **Decision:** `listener.deployment_id` is a denormalized, **set-once** FK: parent fields
  (`organization_id`/`deployment_id`/`pod_id`/`aggregator_id`) are create-only across the
  whole hierarchy — no re-parenting in v1, PATCH models reject them (`extra="forbid"`) — so
  the stamp is computed server-side at create (aggregator→pod→deployment walk) and cannot
  drift. It exists because spec 4.3's "listener name unique within its Deployment" must be a
  real constraint (phase-1 §2: "constraint plus application check") and a unique constraint
  cannot span a 3-hop join. `aggregator_uuid` gets a plain **global** UNIQUE: v1 is
  single-organization (spec 12.1), so global uniqueness implies the within-org rule with no
  denormalized org column anywhere.
- **Spec 12.1 reconciliation:** 12.1 forbids stamping the *tenant* id on every table. One
  deployment id on exactly one table is not a tenant stamp; no `organization_id` is
  denormalized anywhere. Multi-org later relaxes the aggregator_uuid constraint to a
  composite in one migration (cross-reference D34's single-org clamp — both must move
  together).
- **Rejected:** triggers (invisible to autogenerate, unnamed by the convention); app-level
  checks alone (phase doc demands a constraint); composite-FK chains (redundant once parents
  are immutable).
- **Reference:** phase-1 §2 fixed choices; spec 4.2/4.3, 12.1; test_hierarchy_schema.py.

## D31 (2026-08-02): MAC is the listener primary key, literally

- **Decision:** `listener.mac String(17)` is the PRIMARY KEY, CHECK-constrained to uppercase
  colon-separated form; the API normalizes case/separators at the boundary. No surrogate
  UUID. Spec 4.2 calls MAC "the immutable primary identity for a Listener across the whole
  platform"; the phase doc's fixed choice is "Listeners key by MAC"; `session.id` is the
  in-repo natural-PK precedent; `audit_log.entity_id` was sized for a MAC from E0.8.
  Rename-safety is a non-issue: MAC is immutable by spec — PATCH never accepts it, and a
  typo'd MAC is a different physical device, fixed by delete + recreate.
- **Reference:** spec 4.2; phase-1 §2; readiness test `test_audit_entity_id_fits_a_mac_address`.

## D30 (2026-08-02): Hierarchy tables are singular, matching E0; routes stay plural per spec

- **Decision:** `organization`, `deployment`, `pod`, `aggregator`, `listener` — singular,
  like every E0 table — although phase-1 E1.1's task text spells them plural. The naming
  convention templates bake table names into constraint names, so consistency is
  load-bearing; URL collections stay plural exactly as spec 13 writes them (`/organizations`
  …), the split `user` table / `/users` route already established. Table names land verbatim
  in the readiness `E0_TABLES` lock.
- **Reference:** phase-1 §2 E1.1; app/db.py NAMING_CONVENTION; spec 13.

## D29 (2026-08-01): Test changes in the records-hygiene batch — two strengthenings

- **Decision:** two tests change in this batch, both at a green gate and both adding
  assertions rather than removing them. (1) `backend/tests/test_governance.py`
  (`test_planning_documents_unmodified_except_appended_addenda`) gains a non-empty guard on
  the `planning-baseline` file list: D23's rescope iterates `git ls-tree` output, and if the
  tag were renamed or the path misspelled, `ls-tree` exits 0 with empty stdout — the loop
  would run zero times and the invariant would pass having verified nothing. The guard
  (`assert len(baseline_names) >= 7`) mirrors `test_planning_documents_tracked_by_git` and
  makes that failure loud. (2) `frontend/tests/users-admin.test.tsx`: the test named "hides
  the sidebar link from a viewer and shows it to an owner" never asserted the viewer half
  (pre-existing — verified identical at `23eff5d`), and D25 deliberately made the Users link
  visible to every role, so the name documented an invariant the product had abandoned while
  passing vacuously. It is replaced by two tests asserting D25's actual intent: the link is
  visible to a viewer AND to an owner; the viewer's content gating stays covered by the
  existing "denies the page to a viewer" test.
- **Reference:** rule R0 (record test changes); DECISIONS D23, D25; project-changes #12;
  review of `23eff5d..f93f061` (2026-08-01).

## D28 (2026-08-01): Late record — Gate 16 changed a fifth test, and what it means for E0.4's acceptance proof

- **Decision (record-only, no code change):** D26 presents four test fixes at the DES.7 gate
  as the complete inventory ("All four are corrections"). A fifth change shipped in the same
  commit (`5347eeb`) unlisted: `frontend/e2e/theme-swap.spec.ts` was rewritten from injecting
  the alt sheet via `page.addStyleTag(ALT_SHEET)` to driving the real theme toggle, because
  D24 scoped the night sheets to `:root[data-theme="dark"]` and stylesheet injection went
  inert. The rewrite is a net strengthening — 2 e2e tests became 4 (persistence across
  reload; the status palette relit on `/map`) and the swap is now exercised through the path
  a user takes — but it was explained only in the spec file's header comment, and it changed
  what proves E0.4's acceptance criterion: "swapping its values visibly restyles the shell"
  is now demonstrated via `lib/theme.ts` flipping `data-theme`, not via a bare sheet swap
  with zero code changes. Recorded so D26 does not stand as complete and the criterion's
  changed proof is written down (see PHASE0-4-06).
- **Reference:** rule R0 (record test changes); DECISIONS D24, D26; commit `5347eeb`
  (Gate 16); project-changes #12.

## D27 (2026-07-31): Fonts vendored, and the status glyphs get their own 568-byte subset

- **Decision:** the three typefaces the token sheets name ship as latin-subset woff2 files in
  `frontend/public/fonts/`, declared in the new `frontend/src/styles/fonts.css` — IBM Plex
  Sans 400/500/600, IBM Plex Mono 400/600, Source Serif 4 600. Only weights the CSS uses are
  vendored. A **seventh** file, `eoe-status-glyphs.woff2`, carries the six status glyphs, and
  a new additive token `--eoe-font-family-glyph` (D21 terms) points `.status-glyph::before`
  at it.
- **Why the seventh file — the finding that forced it:** the six status glyphs are Geometric
  Shapes and Dingbats codepoints (`●` U+25CF, `◐` U+25D0, `▲` U+25B2, `■` U+25A0, `✕` U+2715,
  `◆` U+25C6), and **none of them exists in IBM Plex Sans, IBM Plex Mono, or Source Serif 4**
  — verified against the *complete* families with fontTools, not merely against these
  subsets. So vendoring the text faces alone would have left every status shape to whatever
  the host happens to have installed. That is the failure the Gate 16 entry saw as a hairline
  `◐` in headless Chromium, and on a minimal air-gapped host (spec §15.1) it degrades to tofu
  — which silently deletes one of the three channels the status vocabulary is built on
  (`docs/INTERFACES.md`, "Status vocabulary"). Shapes are load-bearing, so they are vendored
  like everything else: Noto Sans Symbols 2 (OFL 1.1) subsetted to exactly those six
  codepoints, 568 bytes.
- **Alternatives considered:** (a) swap to glyphs the text families do cover — Plex offers
  `◊`, `✓` and arrows, not six shapes that stay distinct at 10px, so the vocabulary would
  have shrunk to fit the font; (b) draw the shapes in CSS with `clip-path` — no font
  dependency, but it replaces one token per status with a rule per status and breaks the
  `content: var(--…-glyph)` design the sheets already encode. Both were rejected as worse
  than 568 bytes.
- **Gate enforcement:** `frontend/tests/fonts.test.ts` — every `@font-face` src resolves to a
  committed file; no `url(https:…)` or `@import` in any sheet (vendored means vendored); every
  first-choice family in a `--eoe-font-family*` token has an `@font-face`; **the glyph
  subset's `unicode-range` covers every status glyph token**, so a seventh status added later
  without re-cutting the subset fails the gate instead of shipping as tofu; and
  `.status-glyph::before` still names the glyph family.
- **Licensing:** all three families are OFL 1.1; each license text ships beside the fonts as
  the OFL requires (`LICENSE-ibm-plex.txt`, `LICENSE-source-serif-4.txt`,
  `LICENSE-noto-sans-symbols-2.txt`). Whole set ≈160 KB.
- **Reference:** `project_planning/DES-track-handoff.md` "The three rules" item 3; spec §15.1;
  project-changes #10.

## D26 (2026-07-30): Test fixes at Gate (DES.7 batch)

Rule R0 requires recording tests changed at a red gate. All four are corrections, not
weakenings.

- **`tokens.test.ts` check 1** matched named colors anywhere in a declaration, so
  `white-space: nowrap` failed. Now scans the value only; `color: white` still fails.
- **`tokens.test.ts` check 5** forbids components *importing* a night sheet, but matched the
  filename in prose too, tripping on `lib/theme.ts`'s own header comment. Now matches
  `import "…tokens.alt.css"`.
- **`tests/setup.ts` stubs `window.matchMedia`** — jsdom has no media queries, so
  `lib/theme.ts`'s `prefers-color-scheme` probe threw. Reports "not dark". No coverage lost:
  real resolution, override, and persistence are checked in `e2e/theme-swap.spec.ts`.
- **`auth.test.tsx`** asserts the account by accessible name, not text: D25's top bar shows
  an initials avatar and the email is now `aria-label`/`title`. Same invariant, stronger
  form — it checks what a screen reader announces.
- **Reference:** rule R0 on_failure; `frontend-tests` gate run, DES.7 batch.

## D25 (2026-07-30): DES.7 shell restructure — dark top bar, and primary nav lists every destination

- **Decision:** `Shell.tsx` becomes V2·S1's dark top bar with horizontal nav over an optional
  context band, replacing E0.4's left sidebar.
  `project_planning/DES-track-handoff.md` item 4 names this DES.7's one structural change:
  the map needs full viewport width and the breadcrumb needs a permanent home. `shell-sidebar`
  → `shell-topbar`; regions, `aria-label="Primary"`, and routes otherwise unchanged. New
  shared components: `ContextBar`, `PageHeader`, `StatusChip`/`StatusLegend`, `EmptyState`,
  `ThemeToggle`.
- **The consequential half: primary nav lists every destination for every role,** rather than
  hiding entries behind `<Can>` as the E0.7 sidebar did. Hiding a section teaches a wrong map
  of the product and makes a permissions problem look like a missing feature. Pages gate their
  own contents instead (`UsersAdmin` already did), and backend RBAC remains the authority.
  An affordance change, not a security one — no endpoint's protection depended on a hidden link.
- **The four new skeleton pages carry no gate,** deliberately: they display no data, only which
  epic brings the surface. Each gets its gate in that epic.
- **Rejected:** rendering unpermitted entries visibly disabled (the handoff's read of spec
  §12.3). Right once roles are routinely exercised; during the skeleton phase every entry would
  render disabled for a signed-out reviewer. Revisit at DES.8.

## D24 (2026-07-30): Night theme ships — D21's dark-palette gap closed, selector-scoped

- **Decision:** D21 left one gap open — nothing carried dark values for the extension keys, so
  a dark marker, badge, or table cell rendered a near-black status color on a near-black
  surface. `frontend/src/styles/tokens.ext.alt.css` closes it. `tokens.alt.css` stops being a
  test fixture: `main.tsx` imports both night sheets unconditionally and `lib/theme.ts` sets
  `document.documentElement.dataset.theme`.
- **Selector, not import order:** both night sheets are scoped to `:root[data-theme="dark"]`,
  outranking the light sheets' plain `:root`. Reordering imports cannot change which theme
  wins, and nothing is injected or disabled at runtime. Check 10 fails the gate on a bare
  `:root` in either night sheet.
- **Resolution:** a stored choice wins and pins the theme; otherwise `prefers-color-scheme`
  decides and keeps deciding. The manual override is not optional — field staff read this
  outdoors in daylight, where the OS setting is wrong.
- **Color keys only.** Glyphs, spacing, type, density, motion, and border widths are
  theme-independent. Check 9 fails if the night sheet defines a key the light extension does
  not (it would resolve in dark, be undefined in light); check 8 mirrors check 7 so
  `danger`/`success`/`warning` cannot drift from their status aliases in either theme. Every
  status color was contrast-verified per pair against its tint, `surface`, and `bg`; lowest in
  the set is 4.8:1.
- **New keys in `tokens.ext.css`, same D21 terms (nothing renamed or repointed):**
  `--eoe-color-action-contrast-muted`, `-action-raised`, `-accent-on-action`, `-brand-mark` —
  the chrome is `--eoe-color-action` in *both* themes, so anything sitting on it needs an
  on-dark pair; `--eoe-radius-pill`/`-round` (shape constants, not ramp points);
  `--eoe-height-topbar`/`-contextbar` (new `--eoe-height-*` namespace for fixed app furniture,
  which is not a control height).
- **Rejected:** toggling `<link disabled>` at runtime (flash of wrong theme, not statically
  analyzable); a `prefers-color-scheme` media block (no manual override, the requirement that
  matters most here).

## D23 (2026-07-30): Test fix at Gate (DES batch), planning-doc governance check scoped to the actual baseline set

- **Decision:** `backend/tests/test_governance.py::test_planning_documents_unmodified_except_appended_addenda`
  iterated every `*.md` file currently present in `project_planning/` and required each to
  have an identical counterpart in the `planning-baseline` git tag, crashing (not failing
  cleanly) on any file that didn't exist at baseline. This batch adds
  `project_planning/DES.4-handoff.md` and `project_planning/DES-track-handoff.md` — DES-track
  handoff/rationale material, not the fixed spec/plan/handbook/phase documents the baseline
  tag actually pins (implementation-handbook.md section 1's authority order names exactly
  those five kinds of document as "binding"). The test now walks
  `git ls-tree --name-only planning-baseline project_planning/` instead of the live directory
  listing, so it diffs only the documents that were actually part of the frozen baseline.
  New, non-baseline files in `project_planning/` are simply outside what this invariant
  covers — there is nothing in the baseline tree to diff a new file against.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. Not a weakening:
  the seven originally-baselined documents are exactly as protected as before (still diffed
  byte-for-byte outside appended addenda); the test's old behavior of hard-crashing on any
  new sibling file was an artifact of nothing having been added to the directory since E0.0,
  not a deliberate invariant that new files are forbidden.
- **Owner directive:** the project owner asked directly for DES-track handoff/rationale docs
  to live in `project_planning/`, not `docs/` — they are project-planning material, not
  engineering-internal logs. `docs/DES.4-handoff.md` moves to
  `project_planning/DES.4-handoff.md`; `docs/HANDOFF.md` moves to and is renamed
  `project_planning/DES-track-handoff.md` (its content spans DES.1–DES.8, so the generic name
  no longer fit next to a track-scoped one).
- **Reference:** rule R0 on_failure; `backend-tests` gate run during the D21 (DES-4-01) batch;
  `test_planning_documents_tracked_by_git` (unaffected, still a `>= 7` lower bound).

## D22 (2026-07-30): Test fix at Gate (DES batch), theme-swap assertion no longer checks font/spacing

- **Decision:** `frontend/e2e/theme-swap.spec.ts` asserted that `fontFamily` and the
  sidebar's computed `padding` change when `tokens.alt.css` is swapped in. Both assertions
  now fail: the DES.4 v2 night theme deliberately keeps the same type family and the same
  `--eoe-space-*` scale as the light sheet ("relit rather than inverted" — only color and
  shadow values change; see `tokens.alt.css`'s own header comment). The assertions checked
  an artifact of the old *synthetic* alt sheet (E0.4-era: an arbitrary Georgia/mono/zero-radius
  fixture designed so every property category visibly differed), not an actual product
  requirement — nothing in spec 3.2 or the DES track's direction calls for the night theme to
  use a different typeface or rhythm. Replaced with `sidebarBackground`
  (`--eoe-color-surface`) and `sidebarBorderColor` (`--eoe-color-border`), which do differ
  between the two real themes and still prove the swap mechanism (loading the alternate sheet
  changes computed styles with zero code changes) end to end.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. This is a test
  correction, not a weakening: the invariant under test — "swapping token values visibly
  restyles the shell" (E0.4 acceptance criterion) — still holds and is still checked on real
  computed styles; only the specific CSS properties asserted changed, because two of the four
  original properties are no longer expected to differ by design.
- **Reference:** rule R0 on_failure; `frontend-e2e` gate run during the D21 (DES-4-01) batch;
  docs/INTERFACES.md "Design tokens".

## D21 (2026-07-30): DES-4-01 accepted — additive status/border/density token namespaces

- **Decision:** `docs/INTERFACES.md` "Design tokens" fixed five namespaces and DES.4's brief
  was a replacement *value set* for the existing property names only. The six device states
  spec §9.3/§6.2 requires (`streaming/healthy`, `sleeping`, `degraded`, `offline`,
  `alerting`, `drifted`) cannot be built inside the locked `danger`/`success`/`warning` set
  without collapsing distinct states (`sleeping` into `offline`, `drifted` into `failed`),
  which spec §6.5/§6.2 treat as meaningfully different. **Accepted as proposed, additive
  only:** `frontend/src/styles/tokens.ext.css` extends `--eoe-color-*`, `--eoe-space-*`, and
  `--eoe-font-*` with new keys, and introduces new namespaces `--eoe-border-width-*`,
  `--eoe-row-height-*`, `--eoe-control-height-*`, `--eoe-duration-*`, `--eoe-ease`. No
  existing key is renamed, removed, or repointed; `danger`/`success`/`warning` keep their
  names and are aliased to `status-alerting`/`status-healthy`/`status-degraded` so the two
  vocabularies cannot drift apart. Each status carries a color, a tint, and a glyph
  (`--eoe-color-status-{name}`, `-tint`, `-glyph`) — color is never the only channel spec
  §9.3 badges/markers/chips rely on, and the six-value status vocabulary is now closed.
  `frontend/src/main.tsx` imports the sheet; `frontend/tests/tokens.test.ts` treats it as a
  third application-owned sheet (alongside `tokens.css`/`tokens.alt.css`), not a literal
  leak, and check 7 asserts the `danger`/`success`/`warning` values stay byte-equal to their
  status aliases (a real cross-sheet `var()` reference isn't possible without coupling
  `tokens.css` to the extension, so the sync is gate-enforced instead).
- **Rejected alternatives:** reusing `danger`/`success`/`warning` for six states (loses the
  `sleeping`/`offline` and `drifted`/`failed` distinctions spec §6.2/§6.5 require); literals
  in a separate `status.css` module (defeats the DES.7 theme-swap guarantee — a dark theme
  would leave status colors behind); encoding status in a data attribute and resolving color
  in JS (moves theme values into TS, the same gate problem one layer removed).
- **Separable bug fix, included in the same change:** `frontend/src/styles/app.css` wrote
  `border: var(--eoe-space-1) solid …` / `outline: var(--eoe-space-1) solid …` in four places
  for want of a width token, rendering the sidebar border, `.card` border, and both
  focus-visible outlines at **4px** instead of a hairline. All four now use the new
  `--eoe-border-width-hairline: 1px`.
- **Known gap, deliberately deferred:** `tokens.alt.css` (the night theme) does **not** yet
  mirror the keys `tokens.ext.css` adds. `tests/tokens.test.ts` check 6 still only compares
  `tokens.css` against `tokens.alt.css`, so this is not gate-enforced yet either. Producing
  correct dark-mode status colors requires per-pair contrast verification the way the three
  existing status-aliased colors got (spec'd, not just scaled) — that is real design work, not
  a mechanical follow of this decision, and is out of scope for this batch. Do not assume the
  night theme has a status palette until a follow-up decision closes this gap.
- **Rationale:** Rule R2 requires flagging a change to an E0-owned interface before applying
  it; DES.4-handoff.md was that flag, raised by the DES track. The project owner accepted it
  in this session as part of finishing the DES.4 delivery — additive-only, so every current
  E0 consumer of the five locked namespaces is unaffected and the E0.4 acceptance criteria
  keep holding.
- **Reference:** project-changes #8; project_planning/DES.4-handoff.md; docs/INTERFACES.md "Design
  tokens"; spec sections 9.3, 6.2, 6.5; phase-0-foundations.md section 2 (E0.4).

## D20 (2026-07-24): Verifier cleanup semantics and httpx promotion

- **Decision:** The deployment verifier (`app/verify.py`) deletes the temporary accounts it
  creates via direct database operations (the API deliberately has no user-delete surface,
  spec 13), in FK order: sessions, role assignments, the `totp:{id}` secret row, then the
  user. **Audit rows are never deleted**: the `ondelete=SET NULL` actor FK clears their
  actor reference and the verification trail remains permanently — immutability outranks
  tidiness, and the guide documents this as an implication. `httpx` moves from the dev
  group to main dependencies (the shipped verifier needs it).
- **Rationale:** "Delete the specific account we create" (owner directive) is satisfied at
  the account level while preserving the audit invariant every other part of the platform
  enforces.
- **Reference:** project-changes #7; guide/verify-deployment.md; spec sections 13, 14.1.

## D19 (2026-07-24): Pre-E8 hardening pulled forward by the readiness flight

- **Decision:** Two production-posture fixes land with the E0-R readiness flight rather
  than waiting for E8.7: the API image runs as a fixed non-root user (UID 10001), and the
  compose frontend service now receives `VITE_API_BASE_URL` (default
  `http://localhost:8000`, overridable via `EOE_FRONTEND_API_URL`).
- **Rationale:** Owner directive to verify a production-poised platform. The missing
  frontend env var was a genuine defect: inside the compose stack the browser app could
  never reach the API (only the Playwright config set the variable, out-of-band). Root
  containers are a needless posture risk with a two-line fix.
- **Reference:** project-changes #6; E8.7 still performs the full security review.

## D18 (2026-07-24): Secret scan covers untracked files; fixture credentials are generated

- **Decision:** Two changes after CI (correctly) went red on the E0.6 push while the local
  gate had passed. (1) Test fixture credentials are generated per run
  (`PASSWORD = f"pw-{uuid4().hex}"`), never committed as literals; the scanner's flag on
  `correct-horse-battery` was upheld, not allowlisted. (2) The secret scan now walks
  `git ls-files --cached --others --exclude-standard`, so untracked files are covered at
  the gate that introduces them instead of only after their close-out commit.
- **Rationale:** Rule R0 requires recording test changes at a red gate; both changes
  strengthen the check. The local/CI divergence existed because gates run before the
  close-out commit while CI runs after it: new files were invisible to a tracked-only scan
  locally.
- **Reference:** rule R2 (secrets never in fixtures); CI run on `e0-batch-3` at gate-6;
  backend/tests/test_repo_layout.py, backend/tests/test_auth.py.

## D17 (2026-07-24): Branch protection pending repository-owner action — RESOLVED 2026-07-30

- **Decision:** The API attempt to require the `ci-green` status check on `main` returned
  404 (GitHub's masking of missing admin rights; the working account has WRITE). The
  pipeline is fully functional without it; hard merge-blocking waits on the repo owner.
- **Verified empirically (same day, after E0.12):** `main` reports `protected: false`; the
  working account's permissions are `admin: false, maintain: false, push: true`; and a
  scratch draft PR (#4, since closed, branch deleted) with deliberately red checks —
  `backend-quality`, `backend-tests`, and `ci-green` all FAILURE — reported
  `mergeStateStatus: UNSTABLE, mergeable: MERGEABLE`. **GitHub would currently allow a red
  PR to merge.** Detection works end to end; enforcement is the single missing piece and it
  is exactly the one-checkbox owner action below. Until it is applied, merge discipline is
  procedural (rule R3: never merge a red PR).
- **Action for the repository owner (HudsonReynolds2):** Settings → Branches → Add branch
  protection rule → branch pattern `main` → enable "Require status checks to pass before
  merging" → select **`ci-green`** (only this one; it fans in every stage, so newly added
  stages block automatically without touching settings again). Optionally also enable
  "Require a pull request before merging".
- **Resolved (2026-07-30; recorded 2026-08-01):** The repository owner applied a
  "protect-main" ruleset requiring the `ci-green` check. Verification PR #5
  (`test/ci-gate-verification`, closed, branch deleted) confirmed enforcement in both
  directions — merge blocked while `ci-green` was red, unblocked once green — after one
  correction: the ruleset initially named the check `CI / ci-green` (the
  workflow-qualified display name), which never matches; it was corrected to plain
  `ci-green`. Re-verified 2026-08-01 from this machine: `main` reports `protected: true`,
  and PRs #6 and #7 merged through the required check. GitHub-side merge-blocking is
  active; rule R3's procedural discipline is no longer the only guard. (The protection
  API still returns 404 for the working account — reading ruleset config needs admin.)
- **Reference:** phase-0-foundations.md section 4 (E0.5 acceptance, "a failing test blocks
  merge"); docs/INTERFACES.md "CI pipeline"; PR #5 closing comment.

## D16 (2026-07-24): Line endings pinned to LF via .gitattributes

- **Decision:** `.gitattributes` pins every text file to LF in the repository and the
  working tree on all platforms (`* text=auto eol=lf`), with CRLF only for `*.ps1`/`*.bat`
  and binary patterns exempted. History renormalized with `git add --renormalize`.
- **Rationale:** Gate 5 went red when a branch switch on Windows (core.autocrlf=true)
  smudged CRLF into the working tree and Prettier correctly flagged every file. Without the
  pin, formatting checks disagree between Windows checkouts and the LF-native CI runners,
  making the pipeline flaky by construction.
- **Reference:** rule R0 on_failure; Gate 5 first run log; task E0.5.

## D15 (2026-07-24): CI shape, single workflow over a stage registry with a fan-in check

- **Decision:** One workflow (`.github/workflows/ci.yml`) whose jobs each invoke a single
  stage from the canonical registry in `gate.sh`, ending in a `ci-green` fan-in job that is
  the sole required status check. Everything runs on every push with a per-ref concurrency
  cancel; no path filters. Docker layer caching for the containers job and path filtering
  are recorded future optimizations, deliberately not built now.
- **Rationale:** The registry gives zero drift between CI and the local gate (same shell
  functions execute in both), which is what keeps the pipeline honest as later epics add
  suites (sim-protocol, controlplane-integration). The fan-in gives branch protection one
  stable check name so adding a stage never requires touching repository settings. Full runs
  on every push favor correctness over minutes at the current scale.
- **Reference:** phase-0-foundations.md section 4 (E0.5); docs/INTERFACES.md "CI pipeline";
  closes D9's deferral (the literal alembic reversibility commands now run in CI as the
  `migrations` job).

## D14 (2026-07-24): Test fix at Gate 3, prefix discipline asserted through the public surface

- **Decision:** The prefix-discipline test reads the OpenAPI schema (every documented path
  starts with `/api/v1`, health present) and behaviorally proves nothing serves outside the
  prefix (`/` and `/health` return 404). It does not walk router internals. Invariant
  unchanged.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. Two attempts at
  walking `app.routes` failed against current FastAPI, which represents included routers as
  lazy pathless containers and applies prefixes at match time, leaving route objects with
  unprefixed paths. The public surface (schema plus observable behavior) is the stable,
  version-proof thing to assert.
- **Reference:** rule R0 on_failure; Gate 3 first and second run logs.

## D13 (2026-07-23): Ephemeral test Postgres via direct docker run, not the testcontainers library

- **Decision:** The migration suite starts its ephemeral Postgres with a direct `docker run`
  through the already-proven `docker_cli()`/`docker_env()` helpers (port 54329, random
  password, forced removal on teardown) instead of the `testcontainers` library D6 named.
- **Rationale:** testcontainers-python reaches the daemon through docker-py, whose Windows
  named-pipe transport adds a pywin32 dependency and a second connection path to debug. The
  direct approach reuses one code path for all Docker interaction and gives the same
  guarantee: a real, disposable Postgres per test module.
- **Reference:** amends D6; docs/migration-conventions.md; backend/tests/test_migrations.py.

## D12 (2026-07-23): Test fix at Gate 1, docker CLI directory appended to subprocess PATH

- **Decision:** Integration-test helpers append the docker CLI's own directory to the
  subprocess PATH (`docker_env()` in `backend/tests/test_repo_layout.py`). Assertion logic
  unchanged.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. Docker Desktop's
  `credsStore` invokes `docker-credential-desktop` from PATH; a shell environment captured
  before the install cannot resolve it, failing every image pull with "error getting
  credentials" even though the daemon runs fine.
- **Reference:** rule R0 on_failure; Gate 1 first run log.

## D11 (2026-07-23): Gate enforcement lives in a runner wrapper, not the conftest hook

- **Decision:** The R0 hard-failure on skipped/xfailed/deselected tests is enforced by
  `backend/tests/gate_runner.py`, which the gate scripts invoke; it runs `pytest.main()` with
  no filter arguments, reads the counts through a plugin object, and controls the process exit
  code itself, failing closed if the counts cannot be read. The conftest hook keeps a loud
  advisory print for plain `pytest` runs.
- **Rationale:** Manual verification at Gate 0 showed pytest 9.1.1 ignores mutation of
  `session.exitstatus` inside `pytest_sessionfinish`: the violation printed but the run exited
  0, which would have made the R0 guard silently decorative.
- **Reference:** rule R0; plan section A3; Gate 0 manual verification.

## D10 (2026-07-23): Test fix at Gate 0, explicit UTF-8 subprocess decoding

- **Decision:** The Gate 0 test helper running git subprocesses decodes output with
  `encoding="utf-8"` explicitly (shared `run_git` in `backend/tests/conftest.py`) instead of
  `text=True`, which on Windows decodes with the ANSI code page (cp1252) and crashed on the
  spec's UTF-8 architecture diagram. Assertion logic unchanged.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. This was a
  platform-dependent defect in test infrastructure, not a weakening: the affected test now
  actually executes its comparison on all platforms.
- **Reference:** rule R0 on_failure; Gate 0 run log (project-updates entry for E0.0).

## D9 (2026-07-23): "In CI" acceptance criteria deferred to E0.5

- **Decision:** E0.2's `upgrade head` / `downgrade -1` checks and the container builds are
  verified locally at the gates; wiring the identical commands into GitHub Actions is E0.5's
  job and E0.5 is outside the current batch (E0.0 through E0.4).
- **Rationale:** The batch boundary was set by the project owner. Recording the deferral keeps
  it visible rather than looking like a missed acceptance criterion.
- **Reference:** phase-0-foundations.md section 4, E0.2 and E0.5.

## D8 (2026-07-23): Error code vocabulary

- **Decision:** Error envelope `code` values are snake_case, stable, and never renamed:
  `validation_error`, `unauthorized`, `forbidden`, `not_found`, `method_not_allowed`,
  `conflict`, `internal_error`. The vocabulary may extend; existing codes never change.
- **Rationale:** The phase document fixes the envelope shape but not the `code` vocabulary.
  Fixing it now prevents drift across seven later epics.
- **Reference:** phase-0-foundations.md section 2 (API conventions); spec section 13.

## D7 (2026-07-23): List response envelope and sort grammar

- **Decision:** All list endpoints respond with
  `{"items": [...], "total": int, "limit": int, "offset": int}`. Sort syntax: `sort=name`
  ascending, `sort=-created_at` descending, comma-separated for multi-key. Binding on E1
  through E7.
- **Rationale:** The phase document fixes the request params (`limit`, `offset`, `sort`) but
  not the response shape or sort grammar.
- **Reference:** phase-0-foundations.md section 2 (API conventions); spec section 13.

## D6 (2026-07-23): Toolchain

- **Decision:** Backend: `uv` with `pyproject.toml`, `ruff` (lint and format), `mypy`,
  `pytest`, `testcontainers` for a real ephemeral Postgres. Frontend: `vitest` with React
  Testing Library and `msw`, ESLint and Prettier, `tsc --noEmit`, and a small `playwright`
  suite for real-browser checks.
- **Rationale:** Playwright is required, not optional: E0.4's acceptance criterion (swapping
  token values visibly restyles the shell) needs real computed styles, which jsdom cannot
  resolve for CSS custom properties.
- **Reference:** phase-0-foundations.md sections 2 and 4 (E0.4).

## D5 (2026-07-23): Config file format is TOML

- **Decision:** The optional settings file (spec section 15.3) is TOML, read with stdlib
  `tomllib`. Environment variables override file values; file values override defaults.
- **Rationale:** Zero added dependency on Python 3.12; the precedence rule is the phase
  document's own acceptance criterion.
- **Reference:** phase-0-foundations.md section 4 (E0.3); spec section 15.3.

## D4 (2026-07-23): CSRF via double-submit token

- **Decision:** Sessions ride an `HttpOnly; SameSite=Lax` cookie (Secure except on plain-HTTP
  localhost) plus a double-submit CSRF token. The middleware hook point and `EOE_CORS_ORIGINS`
  setting land in E0.3; token issuance and validation land with E0.6.
- **Rationale:** D2 makes every browser request cross-origin, and cookie auth cross-origin
  requires CSRF protection. No planning document mentions CSRF anywhere.
- **Reference:** phase-0-foundations.md section 4 (E0.6); spec section 14.1.

## D3 (2026-07-23): Audit immutability enforced at app level plus DB grants

- **Decision:** No ORM or endpoint update/delete path for `audit_log`, plus a reversible
  migration that REVOKEs UPDATE and DELETE on the table from the application role.
  Implemented in E0.8 (not the current batch).
- **Rationale:** Meets the phase document's stated bar and survives a future session that
  forgets the invariant. An intentional strengthening, recorded here.
- **Reference:** phase-0-foundations.md section 4 (E0.8); spec sections 14.1 and 13.

## D2 (2026-07-23): Fully decoupled frontend

- **Decision:** The frontend is its own container with its own Dockerfile, tests, lint, and
  typecheck. The API never serves frontend assets in any environment. Production frontend is
  an nginx-served static build (CDN-ready for E8). No Vite dev proxy. The API base URL comes
  from `VITE_API_BASE_URL`. MSW mocks the API for frontend dev and tests. The only coupling
  is the OpenAPI contract.
- **Rationale:** Project owner requirement: the frontend must be as decoupled as possible for
  parallel development and management at scale. No dev proxy means cross-origin behavior is
  exercised from day one instead of appearing first in production.
- **Reference:** phase-0-foundations.md section 4 (E0.1, E0.4); spec sections 15.1 and 3.2.

## D1 (2026-07-23): DB-backed session rows

- **Decision:** Sessions are rows in a `session` table (id, user_id, created_at, expires_at,
  revoked_at, user_agent, ip); the cookie carries a signed opaque session id. Implemented in
  E0.6 (not the current batch); recorded now so E0.2's baseline and INTERFACES.md anticipate it.
- **Rationale:** Makes `POST /auth/logout` actually revoke, and lets E0.9 invalidate a
  deactivated user's sessions immediately. Still satisfies "signed expiring session tokens".
- **Reference:** phase-0-foundations.md section 4 (E0.6); spec section 12.2.
