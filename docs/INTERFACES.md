# Interfaces

The growing contract between phases (implementation-handbook.md section 1). Every phase
reads this file first and appends what it owns. Do not change a section owned by an earlier
phase without flagging the change explicitly first. Section references (spec sections and
phase-doc tasks) are given so a fresh session can verify any entry at its source.

---

## Owned by E0

### Repository layout (E0.1; phase-0 section 2)

```
/backend        FastAPI app (package name: app), alembic/, tests/
/frontend       Vite React TS app
/deploy         docker-compose.yml and env templates (stack templates arrive in E5)
/sim            reserved for the simulation harness (SIM epic)
/docs           INTERFACES.md, DECISIONS.md, project logs, migration conventions
```

Dev ports: API 8000, frontend dev server 5173, Postgres 5432, Redis 6379.

### Environment variables (E0.1, E0.3; phase-0 section 2)

| Name | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection URL |
| `EOE_SESSION_SECRET` | yes | signs session cookies |
| `EOE_KEK` | yes | base64 platform key-encryption key (SecretStore, E0.11) |
| `REDIS_URL` | no | enables Redis-backed features (E3, E7) |
| `EOE_CORS_ORIGINS` | no | comma-separated allowed browser origins (D2/D4) |

No secret defaults are committed; `deploy/.env.example` documents names only, never values.
Settings precedence: environment variable over TOML config file over default (D5).

### API conventions (E0.3; phase-0 section 2; spec section 13)

- Versioned prefix `/api/v1` on every route, including docs (`/api/v1/docs`,
  `/api/v1/openapi.json`); nothing serves outside it. Health: `GET /api/v1/health` returns
  `{status, version, build_sha, database}`; `build_sha` is injected at image build
  (`BUILD_SHA` arg); an unreachable database degrades the payload, never fails the endpoint.
- Serve with `uvicorn app.main:create_app --factory`; the factory takes an optional
  `Settings` for tests.
- Error envelope, the only error shape:
  `{"error": {"code": string, "message": string, "detail": object|null}}`.
- Error `code` vocabulary (D8, stable, never renamed): `validation_error`, `unauthorized`,
  `forbidden`, `not_found`, `method_not_allowed`, `conflict`, `internal_error`.
- Every list endpoint accepts `limit`, `offset`, `sort`, and filter params, and responds
  with `{"items": [...], "total": int, "limit": int, "offset": int}` (D7).
- Sort grammar (D7): `sort=field` ascending, `sort=-field` descending, comma-separated
  multi-key.
- Request IDs: middleware generates or honors an inbound `X-Request-ID`, echoes it on every
  response, and binds it into structured logs. `audit_log.request_id` (E0.8) consumes it.
- The frontend couples to the backend only through the OpenAPI contract (D2).

### Migration conventions (E0.2; docs/migration-conventions.md)

Append-only history; every migration reversible with a real `downgrade()`; autogenerate
reviewed by hand; exactly one head at all times. SQLAlchemy `MetaData` carries a fixed
constraint naming convention; all constraints are named through it.

### Design tokens (E0.4; spec section 3.2)

Token namespaces (binding target format for DES.4): `--eoe-color-*`, `--eoe-space-*`,
`--eoe-font-*`, `--eoe-radius-*`, `--eoe-shadow-*`. The token sheet is the single file
**`frontend/src/styles/tokens.css`** (neutral defaults; DES.4 delivers a replacement value
set for exactly its custom-property names). `frontend/src/styles/tokens.alt.css` is a
test-only fixture proving the swap and must mirror the exact key set. Every component
styles through `var(--eoe-*)`; color, spacing, radius, and shadow literals outside the
sheet fail the gate (`frontend/tests/tokens.test.ts`). The theme-swap browser test
(`frontend/e2e/theme-swap.spec.ts`) is DES.7's regression guarantee.

### CI pipeline (E0.5; .github/workflows/ci.yml)

The merge watchtower for every later phase. Design contract:

- **Stage registry:** `gate.sh` is canonical. Each stage is a function; `sh gate.sh <stage>`
  runs one stage, `sh gate.sh` runs the local set, `sh gate.sh --list` prints the registry.
  Every CI job invokes exactly one registry stage, so CI and the local gate execute the same
  code; `backend/tests/test_ci_pipeline.py` enforces the parity in both directions.
- **Job naming:** `<area>-<kind>` (`backend-quality`, `frontend-e2e`, ...). Future areas
  slot in beside them: `sim-protocol` (listener/aggregator contract suites, SIM epic),
  `controlplane-integration` (E3), and so on.
- **Adding a stage, the 3-step recipe:** (1) stage function plus `STAGES` entry in
  `gate.sh`; (2) job invoking `sh gate.sh <stage>` in `ci.yml`; (3) the job id added to the
  `ci-green` needs list. The parity test fails the gate if any step is skipped.
- **`ci-green` fan-in** is the single required status check for branch protection; it runs
  with `if: always()` and fails (never skips) when any dependency fails, because GitHub
  treats a skipped required check as satisfied.
- Everything runs on every push (no path filters at this scale; recorded future option,
  DECISIONS D15). Rule R0 applies inside CI: `backend-tests` runs `tests/gate_runner.py`,
  so skipped/xfailed/deselected tests fail the pipeline.

### Auth and session mechanics (E0.6)

- **Tables:** `user` (id UUID, email unique+indexed, password_hash Argon2id, is_active,
  created_at) and `session` (opaque 43-char id PK, user_id FK, csrf_token, created_at,
  expires_at, revoked_at, user_agent, ip) — migration `c07e17281417`. Model classes:
  `app.models.User`, `app.models.UserSession`.
- **Cookies:** `eoe_session` = `<session_id>.<hmac-sha256(session_id, EOE_SESSION_SECRET)>`,
  HttpOnly, SameSite=Lax, Secure on https (D1); `eoe_csrf` = per-session token, JS-readable
  by design (double-submit, D4). TTL from `EOE_SESSION_TTL_SECONDS` (default 43200).
- **Endpoints:** `POST /api/v1/auth/login` (one indistinguishable 401 for any bad
  credential; response carries id/email/is_active, never a password or hash),
  `POST /api/v1/auth/logout` (requires session + `X-CSRF-Token`; revokes the row
  immediately; 204), `GET /api/v1/auth/me` (`/me` is an E0 addition to the spec-13 surface
  for frontend session state).
- **Dependencies for later phases** (`app/auth/deps.py`): `get_db` yields a SQLAlchemy
  session from `app.state.session_factory`; `require_session` (SessionDep) validates the
  signed cookie and loads a live row; `require_csrf` (CsrfSessionDep) enforces the
  double-submit header on mutations. E0.7's RBAC dependency composes on top of
  `require_session`; every later mutating endpoint uses `CsrfSessionDep` (or its RBAC
  wrapper) plus the E0.8 audit hook when it lands.
- **Password hashing:** `app/auth/passwords.py`, argon2-cffi defaults; plaintext exists
  only inside the login request scope; never logged (tested).

### RBAC roles and the permission dependency (E0.7; placeholder)

Roles fixed by spec section 12.3: `owner`, `deployment_operator`, `field_tech`, `viewer`.
Deployment-scoped assignment with a nullable scope column until E1 adds the foreign key.
Details land with E0.7.

### Audit hook usage (E0.8; placeholder)

Table columns fixed by phase-0 section 4: id, at, actor_user_id, action, entity_type,
entity_id, scope, detail JSONB, request_id. Every mutation endpoint calls the write hook.
Details land with E0.8.

### SecretStore interface (E0.11; placeholder)

Platform-side envelope encryption per spec section 12.4 (KEK from `EOE_KEK`, data keys per
secret, rotation by re-wrap). Consumers: E4 (device-facing bundle secrets), E5 (service
credentials). NOT the device-facing scheme of spec section 8.4, which E4 owns. Details land
with E0.11.
