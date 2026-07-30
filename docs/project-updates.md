# Project Updates

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
