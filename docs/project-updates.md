# Project Updates

Dated, reverse-chronological, append-only log of verified project state changes (rule R1,
`.claude/rules/project-rules.json`). An entry is written only after its gate passed with
0 failed, 0 skipped, 0 xfailed, 0 deselected, AND the task's manual verification steps ran.
Never in anticipation.

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
