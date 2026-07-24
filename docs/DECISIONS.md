# Decisions

Deviations from the spec or a phase document, and implementation choices the documents left
open, with rationale (implementation-handbook.md section 1, rule R1). Feed these back into
the next spec or phase-doc revision. Newest first within each batch.

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
