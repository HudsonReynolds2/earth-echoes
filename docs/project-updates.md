# Project Updates

Dated, reverse-chronological, append-only log of verified project state changes (rule R1,
`.claude/rules/project-rules.json`). An entry is written only after its gate passed with
0 failed, 0 skipped, 0 xfailed, 0 deselected, AND the task's manual verification steps ran.
Never in anticipation.

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
