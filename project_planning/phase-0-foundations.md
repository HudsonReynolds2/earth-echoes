# Phase 0 Document: Foundations (Epic E0)

**Companion documents:** Technical Specification v1.1 (authoritative), Project Development Plan v1.0
**Spec sections implemented:** 12, 15 (plus the token structure from 3.2)
**Depends on:** nothing. This is the first implementation phase.

---

## 1. Scope

Build the running skeleton every later phase adds to: repository layout, containerized dev environment, PostgreSQL with migrations, a FastAPI backend, a React frontend with the neutral design-token structure, CI, local authentication, RBAC, the audit log, user administration, optional TOTP, and the platform-side secrets envelope encryption layer. Spec sections cited in the tasks are binding; where this document fixes a concrete choice the spec left open (names, ports, library picks), that choice is also binding so later phases inherit a stable base.

## 2. Prerequisites and inherited interfaces

None. This phase creates the interfaces others inherit. Everything this phase decides must land in `docs/INTERFACES.md` (see Handoff artifacts).

Fixed choices for this phase:

- **Languages and frameworks:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2, pytest on the backend. Node 20+, Vite, React 18, TypeScript, TanStack Query on the frontend.
- **Repository layout (monorepo):**
  ```
  /backend        FastAPI app (package name: app), alembic/, tests/
  /frontend       Vite React TS app
  /deploy         docker-compose.yml and env templates (stack templates arrive in E5)
  /sim            reserved for the simulation harness (SIM epic)
  /docs           INTERFACES.md, DECISIONS.md
  ```
> **Addendum PHASE0-2-01 (2026-07-24, ref project-changes #7):** The monorepo layout gains a top-level `guide/` directory holding every client-facing artifact (operator quickstart, seed-script usage and implications, deployment-verifier instructions), kept deliberately separate from the engineering-internal `docs/`. E0.12 additionally ships `backend/app/verify.py`, a client-facing owner-journey verifier documented there.
- **Ports (dev):** API 8000, frontend dev server 5173, Postgres 5432, Redis 6379.

> **Addendum PHASE0-2-02 (2026-08-10, ref project-changes #21):** The published HOST ports moved into the 1xxxx range - API 18000, frontend dev server 15173, Postgres 15432, Redis 16379, and E3.1's MQTT-over-TLS broker 18883 - because the standard numbers collide with services commonly already running on a developer machine, and the collision makes the R0 gate unpassable rather than merely inconvenient (test_compose_stack and test_verify_tool bind them for real). The CONTAINER side is unchanged: every service still listens on its standard port inside the compose network, so no image, process argument, broker listener or service-to-service URL moved. FIXED_PORTS in backend/tests/test_repo_layout.py stays the one enforced list and now holds HOST:CONTAINER pairs. Owner instruction, 2026-08-10.
- **Core environment variables:** `DATABASE_URL`, `EOE_SESSION_SECRET`, `EOE_KEK` (base64, platform key-encryption key), `REDIS_URL` (optional). No secret defaults committed; `deploy/.env.example` documents them.
- **API conventions:** versioned prefix `/api/v1`, health at `GET /api/v1/health`, JSON error envelope `{"error": {"code": string, "message": string, "detail": object|null}}`, all list endpoints accept `limit`, `offset`, `sort`, and filter params (spec 13).

## 3. Out of scope

Do not build any of the following; a later epic owns each. Hierarchy entities and CRUD (E1). The settings catalog, overrides, or any config merge (E2). MQTT, brokers, or reconciliation (E3). Provisioning bundles or device-facing KEK/DEK encryption (E4; note E0.11 below is the platform-side scheme of spec 12.4, not the device-facing scheme of spec 8.4). Deployment services onboarding (E5). Maps (E6). Telemetry reads or alerts (E7). Kubernetes manifests and OIDC (E8; this phase only leaves the auth interface pluggable). Any visual design beyond neutral tokens (DES track).

## 4. Task list

**E0.1 Repository and container scaffolding.** Create the monorepo layout above, Dockerfiles for `api` and `frontend`, and `deploy/docker-compose.yml` running `api`, `frontend`, `postgres`, and optional `redis` (spec 15.1). Acceptance: `docker compose up` from a clean clone brings up all services; a top-level README documents dev setup in under ten steps.

**E0.2 Postgres and migrations.** Wire Alembic with an initial migration and document migration conventions: append-only history, every migration reversible, autogenerate reviewed by hand. Acceptance: `alembic upgrade head` and `downgrade -1` both succeed in CI.

**E0.3 FastAPI skeleton.** App factory, settings loaded from environment variables plus an optional config file (spec 15.3), the `/api/v1` prefix, health endpoint, the error envelope, request ID middleware, and Pydantic-at-the-boundary conventions. Acceptance: health returns build/version info; a deliberately bad request returns the error envelope; settings precedence (env over file) has a test.

**E0.4 React skeleton with neutral design tokens.** Vite app with routing, TanStack Query, a base layout shell (sidebar navigation plus content area), and the design-token structure from spec 3.2 as CSS variables with neutral defaults. Token namespaces: `--eoe-color-*`, `--eoe-space-*`, `--eoe-font-*`, `--eoe-radius-*`, `--eoe-shadow-*`. Every component styles through tokens; no hard-coded colors. Acceptance: the token sheet lives in one file; swapping its values visibly restyles the shell; the DES track receives the token names as its target format (plan DES.4).

**E0.5 CI pipeline.** GitHub Actions running lint, typecheck, backend and frontend tests, migration up/down check, and container builds on every push. Acceptance: CI green on main; a failing test blocks merge.

**E0.6 Local accounts and sessions.** Password auth with Argon2id (argon2-cffi), signed expiring session tokens in HTTP-only cookies, `POST /auth/login` and `POST /auth/logout` (spec 12.2), and a login page. Acceptance: passwords never logged or returned; sessions expire; tests cover login, logout, expiry, and wrong-password paths.

**E0.7 RBAC framework.** The four roles from spec 12.3 (`owner`, `deployment_operator`, `field_tech`, `viewer`), a deployment-scoped assignment model (a user-to-deployment role table; deployment IDs are opaque UUIDs until E1 gives them a real table, so use a nullable scope column now and let E1 add the foreign key), a permission-check dependency applied at the API layer on every request, and a frontend helper that hides or disables actions by role. Acceptance: a sample protected endpoint demonstrates all four roles; permission checks have table-driven tests (spec 14.5 names RBAC checks test-critical).

**E0.8 Audit log.** An immutable `audit_log` table (id, at, actor_user_id, action, entity_type, entity_id, scope, detail JSONB, request_id), a write hook every mutation endpoint calls, and `GET /audit` with scope, actor, and action filters (spec 12.1, 13). Acceptance: every mutating endpoint in this phase writes an audit row; the table has no update/delete path in application code.

**E0.9 User administration.** User CRUD, role and scope assignment endpoints (`GET/POST /users`, `PATCH /users/{id}`), and an admin UI page, owner-only. Acceptance: an owner creates a viewer, the viewer logs in and sees read-only UI.

**E0.10 Optional TOTP.** TOTP enrollment and verification for privileged roles (spec 12.2), off by default. Acceptance: an enrolled owner must pass TOTP at login; unenrolled users are unaffected.

**E0.11 Platform secrets envelope encryption.** The spec 12.4 scheme behind a `SecretStore` interface: data keys encrypt secret values, the KEK (from `EOE_KEK` now, secret manager in E8) wraps data keys, and re-wrapping supports rotation. Ciphertext lives in Postgres; plaintext never appears in logs or API responses. Acceptance: round-trip and rotation tests pass; grepping test logs for a known plaintext secret finds nothing; the interface docstring states that E4 (device-facing bundle secrets) and E5 (service credentials) are its consumers.

**E0.12 Seed script.** A dev seed creating the initial owner account (credentials printed once at creation, not stored in the repo). Acceptance: fresh environment to logged-in owner in one command.

> **Addendum PHASE0-4-01 (2026-07-23, ref project-changes #1):** Task E0.0 (governance scaffolding) precedes E0.1: the binding project rules file (`.claude/rules/project-rules.json`), the `CLAUDE.md` rules loader, `docs/project-updates.md`, `docs/project-changes.md`, `docs/DECISIONS.md`, the `docs/INTERFACES.md` skeleton, the git baseline of `project_planning/`, the commit message template, and the gate runner. E0.0 ends in Gate 0, subject to rule R0 like every task.

> **Addendum PHASE0-4-02 (2026-07-23, ref project-changes #4):** The citation "spec 12.1" in E0.8 is a typo: the audit-log requirement derives from spec sections 14.1 (immutable audit log of every mutation and config push) and 13 (`GET /audit`); section 12.1 covers tenancy.

> **Addendum PHASE0-4-03 (2026-07-24, ref project-changes #5):** E0.11 is implemented before E0.10: the TOTP secret is a secret, and the cross-phase convention that secrets move only through `SecretStore` (handbook section 3) requires the store to exist first. Task content unchanged.

> **Addendum PHASE0-4-04 (2026-07-30, ref project-changes #8):** The five token namespaces this task fixes gain an additive, non-breaking extension (DECISIONS D21, DES-4-01): a third sheet, `frontend/src/styles/tokens.ext.css`, adds new keys to `--eoe-color-*`/`--eoe-space-*`/`--eoe-font-*` and introduces `--eoe-border-width-*`, `--eoe-row-height-*`, `--eoe-control-height-*`, `--eoe-duration-*`, `--eoe-ease`. No name from the original E0.4 set is renamed, removed, or repointed, so the acceptance criteria below continue to hold unchanged.

> **Addendum PHASE0-4-05 (2026-08-01, ref project-changes #9):** E0.4's base layout shell, described above as "sidebar navigation plus content area", was replaced at Gate 16 by V2·S1's dark top bar with horizontal nav and a context band (DECISIONS D25; `shell-sidebar` → `shell-topbar`); the primary nav now lists every destination for every role and pages gate their own contents, with backend RBAC unchanged as the authority. The change was recorded in project-changes #9 and `docs/INTERFACES.md` "Shell layout" when it shipped; this addendum brings the phase document itself current, late.

> **Addendum PHASE0-4-07 (2026-08-11, ref project-changes #25):** E0.1's compose-stack checks (`test_compose_stack.py`, and E0.12's `test_verify_tool.py` beside them) now run under a machine-wide lock, because they publish the `FIXED_PORTS` host ports and share the generated `deploy/dev-certs` that mosquitto bind-mounts — both singular per machine, and neither protected by the `xdist_group` that serialises them within one pytest session. The published ports themselves are unchanged and remain the documented operator contract. The rest of the suite gains host-side port-forward probes and a cross-run port-claim registry so several gate runs can share one host (DECISIONS D110).

> **Addendum PHASE0-4-06 (2026-08-01, ref project-changes #12):** Correction to PHASE0-4-04's closing claim. With `tokens.ext.css` (D21) and the shipped night sheets `tokens.alt.css`/`tokens.ext.alt.css` (D24), the acceptance criterion "the token sheet lives in one file" no longer holds literally: there are four token sheets (`docs/INTERFACES.md` "Design tokens"). The criterion's intent survives and is gate-enforced across all four — every component styles through `var(--eoe-*)`, no literals outside the sheets, swapping token values visibly restyles the shell (proven since Gate 16 by the theme toggle driving `data-theme`, see DECISIONS D28, rather than by injecting a swapped sheet).

## 5. Definition of done

> **Addendum PHASE0-5-01 (2026-07-24, ref project-changes #6):** The definition of done additionally includes a cross-cutting readiness suite (`backend/tests/test_e0_readiness.py`) locking the public surface (exact route and table sets), proving env-var documentation parity, exercising the seams later epics consume (E1 scope columns, E3/E5 SecretStore name shapes, E8.5 session-minting), running a data-seeded migration round trip, and verifying production posture (prod frontend image serves; API container non-root; the compose frontend carries `VITE_API_BASE_URL`).

`docker compose up` on a clean machine yields a running API and frontend. An owner logs in (with TOTP if enrolled), creates users, and assigns roles. Every mutation writes an audit row. RBAC blocks and allows correctly across all four roles with tests proving it. The secrets envelope round-trips and rotates under test. CI runs the full check suite. The frontend shell renders entirely through the token sheet.

## 6. Handoff artifacts

- `docs/INTERFACES.md` created, with sections this phase owns: repo layout, env variables, API conventions and error envelope, auth and session mechanics, RBAC roles and the permission dependency, audit hook usage, the `SecretStore` interface, and the design-token namespace list.
- `docs/DECISIONS.md` recording any deviation from this document or the spec, with rationale.
- The migration chain at its initial state, the seed script, and `.env.example`.
- The token sheet file path communicated to the DES track.
