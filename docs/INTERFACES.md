# Interfaces

The growing contract between phases (implementation-handbook.md section 1). Every phase
reads this file first and appends what it owns. Do not change a section owned by an earlier
phase without flagging the change explicitly first. Section references (spec sections and
phase-doc tasks) are given so a fresh session can verify any entry at its source.

---

## Owned by E0

### Repository layout (E0.1; phase-0 section 2)

```
/guide          client-facing group (PHASE0-2-01): quickstart, seed script,
                deployment verification; new operator-facing material goes HERE
/backend        FastAPI app (package name: app), alembic/, tests/
/frontend       Vite React TS app
/deploy         docker-compose.yml and env templates (stack templates arrive in E5)
/sim            reserved for the simulation harness (SIM epic)
/docs           engineering-internal: INTERFACES.md, DECISIONS.md, project logs,
                migration conventions
```

Dev ports, HOST side (amended by E3.1 — PHASE0-2-02, project-changes #21): API 18000,
frontend dev server 15173, Postgres 15432, Redis 16379, MQTT over TLS 18883. Containers
still listen on 8000/5173/5432/6379/8883 internally and in-network URLs are unchanged;
only the published host side moved, so the stack coexists with other local services.
`FIXED_PORTS` in `backend/tests/test_repo_layout.py` is the enforced list.

### Environment variables (E0.1, E0.3; phase-0 section 2)

| Name | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection URL |
| `EOE_SESSION_SECRET` | yes | signs session cookies |
| `EOE_KEK` | yes | base64 platform key-encryption key (SecretStore, E0.11) |
| `REDIS_URL` | no | enables Redis-backed features (E3, E7) |
| `EOE_CORS_ORIGINS` | no | comma-separated allowed browser origins (D2/D4) |
| `EOE_PUBLISH_ENABLED` | no | gates publication AND the API's outbound broker connection (D61, D86); default off until E3.13 |
| `EOE_WORKER_IN_API` | no | run the reconciliation worker inside the API process instead of the `worker` container (E3.7, D59); default off |
| `EOE_TIMEOUT_SWEEP_SECONDS` | no | pending-timeout sweep cadence, default 30 (E3.7) |
| `EOE_DRIFT_SWEEP_SECONDS` | no | drift re-comparison cadence, default 300 (E3.7) |

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
**`frontend/src/styles/tokens.css`** (DES.4 v2, "field notebook" direction, delivers a
replacement value set for exactly these five namespaces' custom-property names; nothing
renamed). `frontend/src/styles/tokens.alt.css` is the shipped night theme (DES.7, D24) and
must mirror the exact key set of `tokens.css`. Every component styles
through `var(--eoe-*)`; color, spacing, radius, and shadow literals outside the token sheets
fail the gate (`frontend/tests/tokens.test.ts`). The theme-swap browser test
(`frontend/e2e/theme-swap.spec.ts`) is DES.7's regression guarantee.

**Additive extension (DECISIONS D21, DES-4-01, 2026-07-30):** a third sheet,
`frontend/src/styles/tokens.ext.css`, is imported by `main.tsx` alongside `tokens.css` and
adds new keys to `--eoe-color-*`/`--eoe-space-*`/`--eoe-font-*` plus five new namespaces:
`--eoe-border-width-*`, `--eoe-row-height-*`, `--eoe-control-height-*`, `--eoe-duration-*`,
`--eoe-ease`. Nothing in the original five namespaces is renamed, removed, or repointed.
`tokens.test.ts` treats `tokens.ext.css` as a third application-owned sheet (not a literal
leak); its keys must stay defined for anything that references them via `var(--eoe-*)`.
DES.7 added `--eoe-radius-pill`/`-round` and a `--eoe-height-*` namespace on the same terms.

**Night theme (DECISIONS D24, 2026-07-30).** Four sheets, not three: `tokens.alt.css` and
`frontend/src/styles/tokens.ext.alt.css` are both scoped to `:root[data-theme="dark"]` and
are imported unconditionally by `main.tsx`, so specificity — never import order — decides
which theme wins. `frontend/src/lib/theme.ts` owns the attribute; **no component may import
a theme sheet** (gate-checked). Resolution is a persisted manual override first,
`prefers-color-scheme` otherwise. `tokens.ext.alt.css` overrides color keys only and must
stay a strict subset of `tokens.ext.css`. Gate checks 8/9/10 enforce the alias parity, the
subset rule, and the attribute scoping respectively.

**Shell layout (DECISIONS D25).** A dark top bar (`data-testid="shell-topbar"`) with a
horizontal `aria-label="Primary"` nav over an optional `ContextBar`, replacing the E0.4
sidebar. The primary nav lists every destination for every role; pages gate their own
contents and the backend RBAC dependency stays the authority.

**Status vocabulary (closed, spec §9.3/§6.2):** exactly six device states —
`streaming/healthy`, `sleeping`, `degraded`, `offline`, `alerting`, `drifted` — each with a
color, a tint, and a glyph token (`--eoe-color-status-{name}`, `-tint`, `-glyph`). The
locked `--eoe-color-danger`/`-success`/`-warning` alias to `status-alerting`/`-healthy`/
`-degraded` respectively and must never be restated with their own hex value, so the two
vocabularies cannot drift apart. D21's known gap is **closed** (D24): both themes carry the
full status palette. Render all three channels — a chip showing only the color is not
accessible; `src/components/StatusChip.tsx` is the canonical implementation.

**Typefaces (DECISIONS D27, 2026-07-31).** Vendored, never fetched — spec §15.1 puts this
platform on air-gapped hosts, so `frontend/src/styles/fonts.css` declares every face from
`frontend/public/fonts/` and `frontend/tests/fonts.test.ts` fails the gate on any
`url(https:…)` or `@import`, on a `--eoe-font-family*` whose first family has no
`@font-face`, and on a status glyph outside the vendored subset's `unicode-range`. Adding a
weight means vendoring that weight; anything else is synthesised. **The six status glyphs
exist in none of the three text families**, so they ship as their own 568-byte subset behind
`--eoe-font-family-glyph` — see `frontend/public/fonts/README.md` before changing a glyph
token.

### Frontend composition and shared components (DES.7; E1/E2/E4/E6 build on these)

The frame is settled; later epics drop data into it rather than restyling it. Everything
below is in `frontend/src/components/` and styled only through tokens.
(`docs/frontend-guide.md` is the working tour of this surface — run instructions, file map,
where-to-change-what; this section is the contract and wins on any disagreement.)

**Page composition — two shapes, pick one.** A routed page renders *either* a standard
scrolling page:

```tsx
<div className="page">
  <PageHeader eyebrow="Hierarchy" title="Inventory">{/* optional actions */}</PageHeader>
  …
</div>
```

*or*, when the surface needs full bleed (the map), a fragment whose first child is a
`ContextBar` followed by its own region. `ContextBar` sits directly under the top bar,
**outside** `.page` — it is the permanent home for the hierarchy breadcrumb (D25), and E1 is
its first real consumer.

| Component | Props | Use it for |
|---|---|---|
| `PageHeader` | `eyebrow`, `title`, `children` (action slot) | Every standard page. Mono eyebrow over a serif title. |
| `ContextBar` | `crumbs: Crumb[]`, `tabs?`, `activeTab?`, `onTabChange?`, `children` | Hierarchy breadcrumb (`{label, to?}`) and sub-view tabs. |
| `StatusChip` | `status: DeviceStatus`, `count?` | **The only** way to render a device state. |
| `StatusLegend` | — | All six states; put it on any surface showing markers. |
| `EmptyState` | `title`, `children`, `testId?` | A surface with no data yet. Say which epic brings it. |
| `Can` / `useCan` | `permission`, `deploymentId?`, `fallback?` | Hiding or disabling writes by role (E0.7). |

**Conventions that are gate-enforced or load-bearing:**

- **No literals.** Color, spacing, radius, shadow, and font-size values come from `var(--eoe-*)`;
  `tokens.test.ts` fails the gate otherwise. A token you need but cannot find is an additive
  extension under D21 (`tokens.ext.css`, plus its dark value in `tokens.ext.alt.css`), never a
  literal and never a rename.
- **New component CSS goes in `app.css`**, in a commented section, not in per-page sheets or
  inline styles. There is no CSS-in-JS in this project.
- **Serif is display only** (`--eoe-font-family-display`): page titles and the one hero metric
  per screen. Never body, never table cells.
- **Mono is for identifiers** (`--eoe-font-family-mono`, class `.mono`): MACs, UUIDs,
  aggregator ids, MQTT topics, checksums, timestamps. E1's tables are full of these.
- **Status is three channels — color + shape + label.** Use `StatusChip`; a bare colored dot
  or a status-colored button is a defect, and no button ever takes a status color.
- **Density comes from tokens:** `--eoe-row-height-compact` (36px) for table rows,
  `--eoe-control-height-*` for inputs and buttons.
- **Tables:** `.admin-table` (E0.9, `UsersAdmin`) is the existing row/header pattern. E1.8's
  TanStack tables should generalise it into shared table classes rather than start a second
  vocabulary; the same goes for forms, where `.auth-form` is login-specific and E1 owns the
  first reusable form styles.

**Surfaces that are deliberately empty** (project-changes #9): Map (E6 owns the engine),
Inventory (E1), Configuration (E2), Provisioning (E4). Each renders a header plus an
`EmptyState` naming its epic and holds **no mock data** — replacing that placeholder with the
real thing is the epic's job. `Screens v2.dc.html` draws V2·S1–S3 only; the remaining screens
are drawn at v1 values in `Screens.dc.html`, and where the two disagree **v2 wins** — take v1
for layout, v2 for every value.

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
- **TOTP (E0.10, optional, off by default):** `POST /auth/totp/enroll` (session+CSRF;
  returns the secret and otpauth URL exactly once; secret stored ONLY as SecretStore
  `totp:{user_id}`) then `POST /auth/totp/confirm {code}` flips `user.totp_enabled`
  (migration `0dd2c6d5b1d2`). Login for enrolled users requires `totp_code`; a missing code
  returns 401 with `detail: {totp_required: true}` (the login page reveals the code field
  on that signal); a wrong code is indistinguishable from bad credentials. Both mutations
  audit (`auth.totp_enroll`, `auth.totp_enabled`).

### RBAC roles and the permission dependency (E0.7)

- **Canonical module:** `backend/app/auth/rbac.py` — `Role` (spec 12.3: `owner`,
  `deployment_operator`, `field_tech`, `viewer`), `Permission` (platform verbs; extend the
  enum and `ROLE_PERMISSIONS` together, deliberately), `has_permission` (pure decision
  core), `require_permission` (dependency factory).
- **Assignment model:** `role_assignment` (migration `658a7e1ad594`): user_id FK, role
  string, `deployment_id` UUID nullable — and since E1.1 (migration `53181716569c`,
  DECISIONS D33) a **real FK to `deployment.id`**, closing the seam phase-0 E0.7 left open.
  **NULL scope = organization-wide grant**; a scoped grant applies only to its deployment;
  an org-level check is satisfied only by an org-wide grant. Unique on
  (user_id, role, deployment_id). Assignment writes pre-validate deployment existence
  (422); orphan grants were deleted, not NULLed, at migration time (D33).
- **Usage on every later endpoint** (spec 12.3: checked at the API layer on every request):
  `Depends(require_permission(Permission.X))` for org-level,
  `Depends(require_permission(Permission.X, "deployment_id"))` to scope by a path
  parameter. Composes `require_session`; mutations still add CSRF (E0.6) and the E0.8
  audit hook when it lands.
- **`GET /auth/me` returns `assignments`** (`[{role, deployment_id}]`) for the frontend.
- **Frontend helper:** `src/lib/rbac.ts` (`can`, `meCan`) plus `src/components/Can.tsx`
  (`<Can>`, `useCan`) hide or disable actions by role. The TS map mirrors the Python
  canon; `frontend/tests/rbac.test.tsx` parses `rbac.py` and fails the gate on divergence.
- **TEST-CRITICAL:** `backend/tests/test_rbac.py` is the RBAC contract (spec 14.5); no
  later session may weaken it.

### Audit hook usage (E0.8)

- **Hook:** `app.audit.record_audit(db, *, action, entity_type, entity_id, actor_user_id,
  scope, detail)` — stages the row on the caller's session and **never commits**; the
  endpoint's single commit seals the mutation and its audit row atomically. Call it in
  every mutation endpoint (universal DoD). The request id binds automatically from the
  middleware contextvar.
- **Caution:** `audit_log.actor_user_id` is a plain FK with no ORM relationship, so the
  unit of work will NOT order inserts — commit a newly created user before auditing with
  their id.
- **Action naming:** `<area>.<verb>` (`auth.login`, `auth.logout`, `user.create`, ...).
  `entity_type`/`entity_id` are free strings (MAC-keyed Listeners fit later); `scope` is a
  deployment UUID, NULL = organization-wide.
- **Immutability:** no update/delete path in application code (tested), plus the migration
  revokes UPDATE/DELETE at the DB layer (D3; binds fully in prod topologies with a
  non-owner app role — E8.7 revisits).
- **Read surface:** `GET /api/v1/audit` behind `Permission.VIEW_AUDIT` (owner-only for
  now), filters `action`, `actor`, `scope`, D7 envelope, default sort `-at`.
- **List-endpoint pattern (binding):** extend `PageParams` with the endpoint's filters into
  one query model (`AuditQuery` style) — FastAPI does not expand a query model mixed with
  loose query params.

### User administration (E0.9)

`GET/POST /api/v1/users`, `PATCH /api/v1/users/{id}` — owner-only
(`Permission.MANAGE_USERS`), mutations require CSRF and audit (`user.create`,
`user.update` with changed-field names only, never values). Assignments replace
wholesale on PATCH. Deactivation revokes the target's live sessions immediately (D1).
Self-lockout guarded: an owner cannot deactivate themselves or drop their own org-wide
owner role (409). List follows D7 with `email` (icontains) and `is_active` filters via the
`UsersQuery` pattern. Admin UI at `/users` behind `<Can permission="manage_users">`;
sidebar link hidden for non-owners.

### SecretStore interface (E0.11)

- **THE ONLY PATH FOR SECRETS AT REST** (rule R2). `app/secrets.py::SecretStore`, reachable
  as `app.state.secret_store`; ciphertext lives in the `secret` table (migration
  `3f3b87c6623f`), which nothing else reads or writes.
- **Scheme (spec 12.4):** fresh 256-bit DEK per write encrypts the value (AES-256-GCM); the
  platform KEK (`EOE_KEK`, base64 of exactly 32 bytes, validated fail-loud at app
  construction) wraps the DEK; `kek_fingerprint` records the wrapping KEK.
  `rotate_kek(new)` re-wraps every DEK without touching values — E8.1 automates this
  against a secret manager behind the same interface.
- **API:** `put(name, plaintext)` (upsert), `get(name)`, `exists(name)`, `delete(name)`,
  `rotate_kek(new_kek_b64) -> count`. Names are namespaced by convention:
  `totp:{user_id}`, `deployment:{id}:{service_key}`, `bundle:{id}:{key}`, and — added by
  E2.2 as a flagged extension of this E0-owned contract (D51) —
  `config:{entity_type}:{entity_id}:{key}` for config override secrets (the listener
  form embeds a MAC, so names carry interior colons; the readiness round-trip covers
  both shapes).
- **Guarantees (tested):** plaintext never in the database, logs, or error messages; GCM
  authentication rejects tampering; a KEK mismatch fails loudly with fingerprints, not
  values.
- **Consumers:** E4 (device-facing bundle secrets held before the separate spec-8.4
  firmware encryption is applied at export — the two schemes nest, they do not compete),
  E5 (deployment service credentials), E0.10 (TOTP secrets), E2.2 (config override
  secrets, D51).

## Owned by E1

### Entity schema (E1.1; spec 4.1-4.3; DECISIONS D30-D33)

Five tables, singular names (D30), all constraints named through the E0.2 convention and
proven by `backend/tests/test_hierarchy_schema.py`:

- **`organization`** — UUID PK; `name` unique; `tags`; timestamps. v1 is single-organization
  (spec 12.1): access scoping flows through the FK chain by join, and **no organization_id
  is denormalized onto any other table**.
- **`deployment`** — UUID PK; FK `organization.id`; `name` unique within its organization;
  **`slug String(63)`** globally unique, CHECK `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$` — the
  `{dep}` MQTT topic segment (spec 7.2). E3: use the slug in topics, never the name or UUID.
- **`pod`** — UUID PK; FK `deployment.id`; `name` unique within its deployment.
- **`aggregator`** — UUID PK; **`pod_id` FK UNIQUE** (`uq_aggregator_pod_id`) — exactly one
  aggregator per pod is a database constraint, not application discipline; three identity
  columns, never conflated (spec 4.2): `id` (platform UUID), `aggregator_uuid String(64)`
  unique + indexed (the join key unifying Prometheus/Influx/S3; global uniqueness = the
  within-org rule while v1 is single-org, D32), `balena_uuid` nullable.
- **`listener`** — **`mac String(17)` is the PRIMARY KEY** (D31), CHECK-constrained to
  uppercase colon-separated `AA:BB:CC:DD:EE:FF`; FK `aggregator.id`; **`deployment_id` is a
  set-once denormalized stamp** (D32) making `uq_listener_deployment_id` (name unique
  within deployment, spec 4.3) expressible — parent fields are create-only across the whole
  hierarchy, no re-parenting in v1; `gps_lat`/`gps_lon` nullable floats are
  **inventory-owned columns** — the E2 settings catalog must register `location.gps_lat`/
  `location.gps_lon` as inventory-resolved: config reads resolve to these columns and no
  override row is ever created for them (spec 5.3; phase-1 E1.1).
- **Timestamps** everywhere: `created_at`/`updated_at` timestamptz, server-default now().
- **Tags** on every entity: `ARRAY(String(64)) NOT NULL DEFAULT []` with a GIN index
  (`ix_<table>_tags`) — the storage model E2's selection engine queries (semantics land
  with E1.7).
- **Migrations:** `ee260dc1c1a8` (tables), `53181716569c` (orphan-grant delete + the
  role_assignment FK). `audit_log.scope` is **deliberately never FK'd** (D3/D33) — a
  readiness test now asserts this permanently.

### Hierarchy API surface (E1.2; spec 13; DECISIONS D34-D36)

- **Routes** (all under `/api/v1`, D7 list envelope + sort grammar, D8 error envelope):
  `GET/POST /organizations`, `GET/PATCH /organizations/{id}` — **no DELETE** (D34), POST
  clamped to one org while v1 is single-org; `GET/POST` + `GET/PATCH/DELETE` for
  `/deployments`, `/pods`, `/aggregators`, and `/listeners` — **listeners addressed by
  MAC** in every path, normalized before lookup (`aa-bb-cc-dd-ee-ff` ==
  `AA:BB:CC:DD:EE:FF` == `aabb.ccdd.eeff`).
- **List filters** (one query model per endpoint, extending PageParams): parent-FK params
  (`organization_id=`, `deployment_id=`, `pod_id=`, `aggregator_id=`), `name=`
  (icontains), `tag=` (exact array containment), plus `slug=` (deployments), `mac=`
  (prefix, listeners), `aggregator_uuid=` (aggregators). SORTABLE: `name`/`created_at`
  everywhere + `slug` (deployments), `mac` (listeners), `aggregator_uuid` (aggregators).
- **One aggregator per pod, one call (E1.3):** `POST /pods` accepts an optional
  `aggregator {aggregator_uuid?, balena_uuid?, name?}` block — create-and-attach in one
  transaction (two audit rows, one commit; a failed aggregator insert rolls the pod back).
  Attaching to an occupied pod via `POST /aggregators` is 409. `aggregator_uuid` is
  platform-assigned (`uuid4().hex`) when omitted.
- **Child counts ride the serializers** (the tree UI needs them without N+1):
  `deployment.pod_count`/`.listener_count`, `pod.listener_count` (+ embedded
  `pod.aggregator`), `aggregator.listener_count`.
- **Deletion:** 409 `conflict` with `detail.children` naming blockers; a deployment
  blocks on pods AND role assignments (D33). No cascades in v1.
- **Slug rule (D36, E3 consumes):** generated from the name when omitted; globally
  unique; **frozen once the deployment has any pod** (PATCH → 409). Known edge for E3 to
  re-examine: pods deleted back to zero unfreezes it pre-E3.
- **Scoped visibility (D35):** `app/scoping.py` — `visible_deployments(assignments,
  permission)`, `scope_filter(statement, column, scope)`, `require_any_assignment`.
  Reads = VIEW_STATUS scoped; child writes = MANAGE_DEVICES in scope + CSRF; org writes +
  POST /deployments = org-level MANAGE_DEVICES. Item-route contract: deployment routes
  403-before-lookup; child items answer identical 404s for out-of-scope and missing
  (MAC-enumeration oracle defense — suite-asserted). Later epics reuse these helpers
  rather than reimplementing visibility.
- **Uniqueness and auto-suffix (E1.4; spec 4.3 item 1):** a listener-name collision
  rejects by default with `409 conflict` and `detail: {"field": "name", "suggestion":
  "<name-2>"}` — the wire shape the E1.8 conflict dialog consumes. `auto_suffix: true` in
  the POST body (an **explicit** parameter, default false, never silent) creates at the
  first free `name-N`, and the audit row records `{auto_suffixed, requested_name,
  final_name}`. A MAC collision always rejects; no parameter overrides it. The
  compute/flush suffix race retries once with a recomputed name, then 409s.
- **Parent fields are create-only across the hierarchy (D32):** PATCH bodies are
  `extra="forbid"` and never accept `mac`, parent ids, or the deployment stamp; create
  bodies forbid unknown fields too, so a client-sent stamp is a 422.
- **Audit:** every mutation writes `<entity>.<verb>` with `scope` = the deployment id
  (org actions: NULL), detail = changed-field names only.

### Bulk import (E1.6; spec 13; D38)

- **Endpoints:** `POST /listeners/import`, `POST /aggregators/import`. Bodies:
  `application/json` `{"rows": [...]}` or raw `text/csv`. Query options: `?partial=true`
  (default false = all-or-nothing) and `?auto_suffix=true` (listeners only, default
  false — E1.4's never-silent rule). Limits: 1000 rows, 1 MiB. Well-formed requests
  answer **200 with a report**: `{committed, created, failed, rows: [{row, status:
  "created"|"error", entity_id, name, error: {code, message}|null}]}` — row codes reuse
  the D8 strings as data. All-or-nothing failure commits nothing, including the audit
  row; the committed=false report is the UI's dry run.
- **CSV formats (normative; header exact, `tags` pipe-separated, blank = null):**
  listeners `mac,name,aggregator_uuid,gps_lat,gps_lon,tags` — parents referenced by
  `aggregator_uuid`; aggregators `pod_id,aggregator_uuid,balena_uuid,name,tags` — blank
  `aggregator_uuid` is platform-generated (spec 4.2). Operator examples:
  `guide/bulk-import.md` (defers here).
- **Scope:** per row — a cross-deployment row is a row-level `forbidden`, not a request
  failure. Audit: one `<entity>.import` row per request (counts, flags, created ids).

### The demo fixture (E1.9; D43) — E2 and E6 reference these rows BY NAME

`uv run python -m app.seed --demo` (idempotence: refuses if the demo org exists; adds
hierarchy-only when an owner already exists). Canonical, deterministic contents —
`frontend/tests/inventory-fixture.ts` mirrors this set exactly:

- **Organization:** "Earth Echoes Demo".
- **Deployments:** "Redwood Coast" (`redwood-coast`, tags `[coastal]`) and "High Desert"
  (`high-desert`, tags `[ridge]`).
- **Pods/aggregators:** Redwood Coast — "Pod 01 · Alder Creek" (`demo-agg-rc-01`, 8
  listeners, `alder-creek-NN`, MACs `02:EE:0E:01:01:NN`), "Pod 02 · Ridge Line"
  (`demo-agg-rc-02`, 5, `ridge-line-NN`, `02:EE:0E:01:02:NN`), "Pod 03 · Tarn Meadow"
  (`demo-agg-rc-03`, 3, `tarn-meadow-NN`, `02:EE:0E:01:03:NN`); High Desert — "Pod 01 ·
  Basin Flat" (`demo-agg-hd-01`, 6, `basin-flat-NN`, `02:EE:0E:02:01:NN`), "Pod 02 ·
  Mesa Rim" (`demo-agg-hd-02`, 4, `mesa-rim-NN`, `02:EE:0E:02:02:NN`), "Pod 03 · Dry
  Wash" (`demo-agg-hd-03`, 2, `dry-wash-NN`, `02:EE:0E:02:03:NN`). 28 listeners total.
- **Determinism rules:** even-index listeners carry GPS (47.6+i/100, −121.88−i/100);
  each pod's first listener carries the pod's tags; nothing is random.

### Inventory frontend (E1.8) — E2/E3/E4/E6 extend these surfaces

- **Routes:** `/inventory` is a nested layout (fragment page shape: ContextBar first,
  then the tree rail beside the routed level) — index = deployments table;
  `deployments/:deploymentId` (pods); `pods/:podId` (aggregator card + listeners — no
  separate aggregator route, one per pod); `listeners/:mac` (inventory facts only);
  `import`. "/" is the Overview roll-up (#16).
- **Vocabulary:** `.data-table` (the generalized table language — do not start another),
  `.form`/`.form-field`/button classes (`.btn-secondary`/`.btn-tertiary`/`.btn-danger`),
  `.tree-*` (rail), `.tag-chip`/`.tag-row`, `.skeleton*` (loading holds real geometry),
  `.modal*`, `.outcome-*` + `tr.row-invalid` (import outcomes — NOT device states),
  `.level-badge`/`.scope-caption`, `.hero-metric`/`.overview-*`. All in `app.css`
  sections; E1.8 tokens added additively: `--eoe-color-danger-border` (+ dark),
  `--eoe-indent-tree`, `--eoe-space-px`, `--eoe-duration-slow`, `--eoe-width-treerail`.
- **Client module:** `src/lib/inventory.ts` — typed `ApiError{code,status,detail}` (the
  409 conflict dialog reads `detail.suggestion`), one function per call, flat query
  keys, mutations invalidate. TanStack **Table** is installed (D39), headless,
  server-driven via the D7 grammar.
- **ContextBar crumbs are real links since E1.8 (D41);** the final crumb carries
  `aria-current="page"`.
- **No fabricated status (D40, gate-enforced):** zero `[data-status]` elements render on
  inventory routes or the Overview until E3 supplies reported state; E3 lifts that guard
  deliberately when StatusChip columns/rollups land. Sort markers, tree carets, and the
  tag-remove "×" (U+00D7) are CSS-drawn/vendored-safe — never glyphs outside the font
  set (D27).

### Tag storage model (E1.7) — E2's selection engine queries this

- **Storage:** `tags ARRAY(String(64)) NOT NULL DEFAULT []` on all five entity tables,
  GIN-indexed (`ix_<table>_tags`). Stored normalized: trimmed, deduplicated, sorted —
  deterministic for selection queries. Validation rejects >64 chars and control
  characters (422).
- **API:** `GET/PUT /{entity}/{id}/tags` on all five entities (listeners by MAC).
  **PUT is wholesale replace, never merge** — `{"tags": [...]}` in, normalized set out.
  Reads follow VIEW_STATUS scoping; writes need MANAGE_DEVICES in scope + CSRF; the D35
  403/404 rules carry over unchanged. Tag writes audit as `<entity>.update` with
  `{"changed": ["tags"]}`.
- **Filtering:** every list endpoint takes `tag=` — exact, case-sensitive containment
  (`tags @> ARRAY[:tag]`, GIN-served). E2's `{"tag": x}` selection predicate compiles to
  the same operator.

### Report-time identity services (E1.5; spec 4.3 items 2-3; D37) — E3.5 calls these

`app/inventory/identity.py`. **E3 wires live MQTT messages into these functions; do not
reimplement their logic.** Signatures, verbatim:

- `ReportedIdentity(mac, aggregator_uuid, name=None, reported_at=None, source="test",
  raw={})` — frozen dataclass; `raw` is preserved verbatim in the quarantine record.
- `handle_reported_identity(db, report: ReportedIdentity) -> IdentityResolution` —
  `IdentityResolution{outcome, listener|None, quarantined|None, alert|None}` with
  `IdentityOutcome` = `MATCHED | NAME_CONFLICT | MAC_CONFLICT | PROVISIONING_REQUIRED |
  UNKNOWN_MAC`. Matching is by MAC; **conflicts never modify inventory rows** — the
  report is quarantined and a `duplicate_identity` alert opens (deduped). UNKNOWN_MAC
  (known reporter, unregistered MAC) has zero side effects: E3 decides per channel.
  Stages rows, never commits — the caller owns the transaction.
- `check_aggregator_membership(db, aggregator_uuid) -> Aggregator | None` — membership
  lookup, never sentinel equality. `require_known_aggregator(db, aggregator_uuid)` is
  the raising variant (`ProvisioningRequiredError`) that also opens the
  `provisioning_required` alert; use it on metrics/analysis/object ingest paths, and
  E3.5 uses it on the reported and event channels too.
- `quarantine_report(db, report, reason) -> QuarantinedReport` — **public since E3.5**
  (was `_quarantine`; behaviour, signature and outcomes unchanged). Stages one quarantine
  row plus its audit row, so a channel deciding what a NON-conflict outcome means reuses
  the row shape rather than assembling its own. `reason` vocabulary: `mac_conflict` and
  `name_conflict` (E1.5), `unknown_mac` (E3.5's reported channel, D76).
- **Tables:** `quarantined_report` (append-only evidence; no FK to listener — survives
  deletion, holds reports about devices inventory never had) and `inventory_alert`
  (open alerts unique per (alert_type, entity_type, entity_key) via partial unique index
  `WHERE resolved_at IS NULL`; `deployment_id` is un-FK'd scope; `resolved_at` NULL =
  open). Alert types are data, not D8 wire codes. E7 unifies alert surfacing later.

---

## Owned by E2

### The settings catalog (E2.1; spec 5.3; D47-D49) — every config consumer reads this

- **Single source:** `backend/app/config/catalog.py::CATALOG` — a frozen constant of 37
  `CatalogEntry` rows matching spec 5.3 key for key (gate-pinned both against a
  hardcoded spec list and against the seeded table, field for field).
  `CATALOG_VERSION = 1`; `LEVELS = (organization, deployment, pod, aggregator,
  listener)` is the merge order, root first.
- **Table:** `settings_catalog` (singular, D30 convention), PK `key`. Columns:
  `value_type` (`int|float|bool|string|object` — `object` is `capture.schedule` only),
  `enum_values` JSONB, `min_value`/`max_value`, `default_value` JSONB (SQL NULL = no
  default; the column name dodges the SQL keyword — the wire field is `default`),
  `lowest_level` (spec literals + `any`, which behaves as `listener` for the level
  rule), `secret` (6 rows), `resolution` (`override|inventory` — `inventory` on
  `location.gps_lat/lon` + `identity.name/mac`, D49: reads resolve from listener
  columns, override writes are rejected naming the key), `write_restricted`
  (`service_onboarding` on all `telemetry.*` + `upload.s3_bucket/s3_endpoint/
  s3_access_key/s3_secret_key`; **`upload.s3_prefix` is deliberately writable** —
  owner ruling, spec 5.1; D48), `notes`, `version`.
- **Catalog evolution rule (D47):** edit the constant + add a sync migration calling
  `seed_catalog(connection)` + bump `CATALOG_VERSION` — always in one batch.
  `seed_catalog` is an upsert-plus-prune that converges on the current constant, so
  history replays and in-place upgrades produce identical tables; never seed the
  catalog any other way.
- **Endpoint:** `GET /config/catalog` (any assignment) → `{"version": N, "items":
  [...]}` sorted by key — a schema document, deliberately not a D7 list (D47). The
  frontend renders ALL config editors from it; a new key must ship with zero frontend
  changes (E2.7's acceptance).

### Override storage (E2.2; spec 5.1; D50-D51) — E2.3 merges these, E2.4 exposes them

- **Table:** `entity_override` (singular, D30) — one row per entity,
  `UNIQUE(entity_type, entity_id)`; `entity_id` is an untyped String (UUID string or
  listener MAC, the audit_log precedent, deliberately un-FK'd); `overrides` JSONB is the
  sparse flat dotted-key map; `catalog_version` stamps the version validated against.
- **Service (`app/config/overrides.py`) — signatures verbatim, stage-never-commit:**
  - `get_overrides(db, entity_type, entity_id) -> dict` — RAW map, markers included;
    never hand it to a response without E2.3 redaction.
  - `put_overrides(db, secret_store, entity_type, entity_id, new_map, *, catalog,
    catalog_version) -> OverrideChange{set_keys, unset_keys, secret_names_to_delete}` —
    wholesale replace (the E1.7 tags precedent), raises
    `OverrideValidationError(errors: [OverrideError{key, code, message}])` before
    staging anything; the API folds errors into ONE 422 `validation_error` with
    `detail {"errors": [...]}` (codes: `unknown_key | inventory_resolved |
    service_restricted | level_rule | invalid_value` — detail vocabulary, not new D8
    wire codes). Callers MUST delete `secret_names_to_delete` through SecretStore
    AFTER their commit (D51 ordering).
  - `delete_overrides_for(db, entity_type, entity_id) -> tuple[str, ...]` — entity-
    deletion cleanup; E2.4 wires it into the four E1 DELETE endpoints; same
    post-commit secret-deletion duty.
- **The level rule (D50; project-changes #17):** at-or-above lowest level, never below;
  `any` = settable everywhere. Validator extras: null is never a value; `object` ≤
  2 KiB; int rejects bool.
- **Secret wire semantics (D51):** stored value = marker
  `{"$secret": "config:{entity_type}:{entity_id}:{key}"}`; redacted reads render
  `{"$secret_set": true}` (the keep sentinel, `app/config/validation.py::KEEP_SENTINEL`);
  PUT takes plaintext string (set/replace) | sentinel (keep; 422 if none stored) |
  omission (unset, deletion post-commit).

### The merge engine (E2.3; spec 5.1, 14.5; D52-D53) — TEST-CRITICAL; E3/E4 consume this

- **Pure core (`app/config/merge.py`) — signatures verbatim:**
  `LevelOverrides{level, entity_id, overrides}` (chain link, root→target, absent levels
  simply absent); `ResolvedValue{value, source, source_entity_id}` (source ∈ LEVELS |
  "default" | "inventory"); `effective_config(chain, catalog, *, target_level,
  inventory=None, inventory_entity_id=None) -> dict[str, ResolvedValue]` (RAW — markers
  verbatim); `redact_secrets(config, catalog)` (set secrets → the keep sentinel);
  `resolve_secret_refs(config, catalog, get)` (plaintext via injected getter —
  INTERNAL ONLY). Semantics (D53): deepest setter wins else default; values replace
  wholesale (objects included); every catalog key at every level except inventory keys
  (listener-only, from columns); unknown/inventory chain overrides ignored on read;
  malformed chains raise; results never alias inputs.
  **tests/test_config_merge.py is the locked documentation of these semantics (rule R0)
  — extend it, never weaken it.**
- **DB accessors (`app/config/service.py`) — pick by audience:**
  `ancestry(db, entity_type, entity_id) -> [(level, id)]` (E1 FK walk; LookupError on
  holes); `override_chain(...)` (one-query row load);
  `effective_for(...)` → **REDACTED** — the only accessor routers may call;
  `effective_raw(...)` → markers verbatim — E2.6 revision snapshots only;
  `effective_resolved(..., secret_store)` → plaintext — **INTERNAL ONLY: E3's publisher
  and E4's bundle generator; wiring it into an HTTP response is a security defect.**
- **Canonicalization + checksum (`app/config/canonical.py`) — FROZEN wire contract
  (D52):** `canonical_config_bytes(snapshot)` = JSON, keys sorted at every depth,
  compact separators, `ensure_ascii=False`, UTF-8, no trailing newline;
  `config_checksum(snapshot)` = `"sha256:" + hexdigest`. Checksums cover snapshots WITH
  markers (secrets never transit desired topics — the snapshot IS the publishable
  payload body, so device-echoed checksums match by construction). Three golden digests
  are pinned in the locked suite; changing any of them is a wire-protocol break.
- **Test dependency:** `hypothesis` (dev-only) runs the suite's property cases under the
  registered `gate` profile (derandomize=True, no deadline — registered in
  tests/conftest.py) so gates stay deterministic.

### Config endpoints (E2.4; spec 13; D35, D50-D51) — the E2.7 editor's data source

- **Routes (×5 entities):** `GET /{entity}/{id}/config/effective`,
  `GET/PUT /{entity}/{id}/config/overrides` (listeners by MAC). Scope discipline is
  E1's verbatim: org reads any-assignment / org WRITE requires an **org-wide**
  MANAGE_CONFIG grant (it changes every deployment); deployments 403-before-lookup
  (VIEW_STATUS read / MANAGE_CONFIG write); pod/aggregator/listener resolve-first →
  identical 404 for missing and out-of-scope (D35). Writes + CSRF.
- **Effective shape:** `{entity_type, entity_id, catalog_version, config: {key:
  {value, source, source_entity_id}}}` — source ∈ level names | "default" |
  "inventory"; every catalog key present (37 at listener, 33 elsewhere); ALWAYS
  redacted (set secrets = the keep sentinel).
- **Overrides shape:** `{entity_type, entity_id, catalog_version, overrides: {key:
  value}}` — the sparse map, secrets rendered as the sentinel. PUT body
  `{"overrides": {...}}` (extra="forbid"), wholesale replace; failures are ONE 422
  `validation_error` with `detail.errors: [{key, code, message}]`, nothing staged.
- **Audit:** `config.override_update`, scope = deployment id (NULL at org), detail =
  `{set: [key names], unset: [key names], catalog_version}` — never values.
- **Deletion cleanup:** the four E1 DELETE endpoints call `delete_overrides_for` and
  delete orphaned config secrets AFTER their commit (D51 ordering).

### The selection engine (E2.5; spec 5.2, 13; D54) — E2.6 preview/apply consumes this

- **Grammar (`app/config/selection.py::SelectionQuery`, every model extra="forbid"):**
  `{entity_type, scope?: {deployment_id}, where?: NODE}`; NODE = `{all: [...]}` |
  `{any: [...]}` | `{tag}` | `{key, op: eq|ne|in, value}` | `{key, op: "exists"}` |
  `{ids: [...]}`. Caps: depth ≤ 5, ≤ 50 predicates. Semantics: tag = E1.7 containment
  parity; eq/ne/in compare EFFECTIVE values (inheritance included; secret keys 422);
  `exists` = override at the entity or any ancestor (inventory keys always false);
  `ids` = explicit membership (the checkbox path — listener ids normalize as MACs).
  Semantic errors (unknown key, secret value query, caps) fold into one 422 with
  `detail.errors` messages naming their keys.
- **Evaluation (`evaluate_selection(db, query, assignments, permission)`):** SQL
  prefilter (type/scope/visibility) + in-Python predicates via the pure merge engine
  with batch-loaded chains (constant query count). ALWAYS re-filters through
  `visible_deployments(permission)` at evaluation time; deterministic order
  (entity_id asc). Saved selections re-evaluate at use — never materialized.
- **Routes:** `POST /selections/preview` (body = the grammar; `limit`/`offset` query
  params; D7 envelope of `{entity_type, entity_id, name, deployment_id, tags}`;
  VIEW_STATUS visibility) · `GET /selections` (D7 list; name filter) ·
  `POST /selections` `{name, query}` → 201 (CSRF + MANAGE_CONFIG in ≥1 deployment;
  409 duplicate name; audit `selection.create` with name + entity_type). **No
  PATCH/DELETE** — spec 13's list, deliberate (D54).

### config_revision — THE E3 HANDOFF (E2.6; spec 6.1, 6.2, 7.3; D55-D56)

- **Table shape, verbatim (E3 inherits this):** `id` UUID PK · `target_type`
  CHECK `('aggregator','listener')` — **per-device only**, pods/orgs never carry
  revisions · `target_id` String(100) (aggregator platform UUID / listener MAC),
  **deliberately un-FK'd** · `deployment_id` UUID indexed, **deliberately un-FK'd**
  (D33 precedent: immutable evidence outlives its subjects — never "fix" this) ·
  `snapshot` JSONB — the device's full effective config, flat dotted keys, secret
  MARKERS never plaintext; **composition rule:** listener snapshots exclude
  `write_restricted` service keys (spec 5.4) and include inventory keys; aggregator
  snapshots include service keys · `schema_version` int = 1 (spec 7.3 payload field) ·
  `checksum` — `config_checksum(snapshot)`, the D52 recipe; **the snapshot IS the
  publishable payload body**, so device-echoed checksums match by construction ·
  `state` String from the spec 6.2 vocabulary — **E2 writes 'draft' ONLY**; every
  transition belongs to E3's machine · `created_by` SET-NULL FK · `created_at`.
  Indexed: (target_type, target_id, created_at), deployment_id, state (pre-pays E3's
  pending scan). **`publish_revision` now exists** (E3.4 — see "The desired publish
  path" under E3), still gated by `EOE_PUBLISH_ENABLED` (Settings.publish_enabled,
  default False until E3.13 per D61; E2's apply only reports it in the response and
  still stops at `draft` until E3.13 wires the call-through).
- **Secret rotation note (D56):** replacing a secret's value keeps the same marker →
  no new revision. Rotation reaches devices via E3's §8.7 rewrap path, never
  desired-config.

### Bulk preview/apply (E2.6; spec 5.2, 14.4; D56) — the E2.8 modal's contract

- **One body for both:** `{selection: <grammar> | {selection_id}, changes: {key:
  value}, level: "target"|"organization"|"deployment"|"pod"|"aggregator"}` (default
  "target"). ONE plan builder computes both — preview==apply is structural. Both
  evaluate through MANAGE_CONFIG visibility. Named level = ONE write at the single
  common ancestor (422 `detail:{level, ancestors}` on a split; org level needs an
  org-wide grant → 403). Changes validate through E2.2 at the write level (same
  errors verbatim).
- **`POST /config/preview`** (no CSRF — mutates nothing): paginated D7-shaped
  envelope, deterministic order (deployment_id, target_type, target_id); items
  `{target_type, target_id, name, pod_id, pod_name, deployment_id, changed_keys,
  no_op, before, after}` — before/after in the redacted ResolvedValue shape; the
  affected set is the honest blast radius (every device under a write target). NO
  status field (D40). Streaming deferred to E8.2 (recorded seam).
- **`POST /config/apply`** (CSRF): ONE transaction — merged override writes at the
  targets, a draft revision per non-no_op device, one `config.apply` audit row per
  affected deployment (detail: changed key names, revision ids, target counts, level).
  Response `{state: "draft", publish_enabled, revisions: [{revision_id, target_type,
  target_id, deployment_id, changed_keys, checksum}]}`.
- **Revisions read surface:** `GET /aggregators/{id}/revisions`,
  `GET /listeners/{mac}/revisions` (D7, default `-created_at`, `state=` filter,
  identical-404; list items omit the snapshot) · `GET /revisions/{revision_id}`
  (full row incl. snapshot; VIEW_STATUS against the row's deployment_id).

### Config frontend (E2.7; D57) — E3/E6 extend these surfaces

- **Routes:** `/configuration` is a nested layout on the shared tree
  (`lib/hierarchy.ts::useHierarchyTree` — the SAME query keys as /inventory, zero
  extra fetches): index = organization editor; `deployments/:deploymentId`;
  `pods/:podId`; `aggregators/:aggregatorId` (own route — overrides write at
  aggregator level); `listeners/:mac`. Tabs Settings/Tags/Revisions ride `?tab=`
  (ContextBar's tab slot, first consumer).
- **Client module:** `src/lib/config.ts` over the extracted `src/lib/http.ts`
  (ApiError re-exported from lib/inventory.ts). PURE helpers carry the logic —
  `editableAt` (the D50 truth table), `provenanceOf`, `buildDraftPut` (the one-PUT
  body: server map + staged edits − reverts), `groupOf`, `unitOf`, `isSecretSet` —
  all unit-tested in config-lib.test.ts.
- **Vocabulary (additive to D42):** `.config-table` on `.data-table` (grouped,
  unsorted, unpaginated — NOT TanStack, by design); `.provenance-chip
  [data-provenance]` (non-status; never data-status — the D40 zero-[data-status]
  guard is test-asserted on config routes); `.toggle` (role="switch", ink track);
  `.draft-banner`; `.draft-diff` (CSS-drawn arrow); `.chain-*`; `.secret-chip`;
  `.config-fact*` (the ListenerDetail card). New tokens (+dark same commit):
  `--eoe-color-warning-border`, `--eoe-width-configrail`.
- **Draft semantics:** edits stage locally, save is ONE wholesale PUT per level;
  the banner counts unsaved keys ("nothing reaches devices until you publish" —
  literally true, publish is E3's; the Publish button renders disabled naming E3 +
  EOE_PUBLISH_ENABLED). Secrets: bullets + set-ness, write-only Replace, diff says
  "replaced"; the sentinel round-trips redacted GETs through PUT.
- **Test fixture:** `frontend/tests/config-fixture.ts` — catalog v4 spanning every
  editor type + the acceptance key `test.demo_knob` (no src/ reference); an
  in-memory override store + miniature D53 merge back the MSW handlers, so
  provenance and no-op detection are real in tests.

---

## Owned by E3

### The MQTT contract module (E3.1, E3.3; spec 7.2, 7.3) — PUBLISHED, SIM CONSUMES IT

- **Path:** `backend/app/contracts/mqtt.py`, under a package whose whole purpose is wire
  contracts shared outside this codebase. **The simulation harness (SIM epic) imports this
  module directly and device firmware is written against it**, so it is published from the
  moment it merged: additive change only, never a silent rename, and a shape change is a
  field-breaking change.
- **Topic builders** — one per spec 7.2 row, all hanging off `deployment_root(dep)` =
  `eoe/{dep}`: `desired_topic`, `reported_topic`, `status_topic`, `event_topic`,
  `command_topic`, `listener_desired_topic`, `listener_reported_topic`, plus
  `aggregator_root`/`listener_root`. `{dep}` is the deployment **slug** (D36), `{agg}` the
  `aggregator_uuid`, `{mac}` a NORMALIZED uppercase colon MAC.
- **Identifiers are validated, never repaired.** `TopicError` (a plain `ValueError`, NOT the
  API's `AppError` — this module runs outside the HTTP layer) rejects anything that fails
  the same formats the database CHECK-constrains: an unchecked slug or MAC reaching a topic
  string could inject a `+`/`#` wildcard and hand one device another's subtree. Normalize at
  the API boundary with `app.inventory.naming.normalize_mac` first.
- **`deployment_subscriptions(dep)`** is the platform's subscribe set: the four
  device-to-platform filters only. Deliberately NOT `eoe/{dep}/#` — that would feed the
  platform's own retained desired publishes back into its consumer.
- **`parse_topic(topic) -> ParsedTopic(kind, dep, agg, mac)`** (added E3.5) is the inverse,
  and the ONLY place a topic is taken apart — `TopicKind` has one member per spec 7.2 row, so
  a caller's `match` covers the table exhaustively. Identifiers are validated on the way in by
  the same functions that validate them on the way out, so a `+`/`#` that reached a topic
  string is refused rather than repaired. Round-trip test-pinned across every builder.
- **`QOS = 1`** on every platform topic (phase-3 fixed choice); retained flags follow the
  spec 7.2 table exactly (desired and status retained; reported, event and cmd not).
- **Payload models** (E3.3; spec 7.3; D67-D68), all on the published base `MqttPayload`:
  `DesiredConfig` (+ `DesiredTarget`), `ReportedAggregatorState` (+ `HealthBlock`),
  `ReportedListenerState` (+ `ListenerLiveness`), `StatusMessage`, `DeviceEvent`, `Command`.
  Helpers: `encode(payload) -> bytes`, `decode(model, raw) -> model`,
  `describe(payload) -> dict` (JSON-ready, for timeline detail and audit rows). Constants:
  `SCHEMA_VERSION`, `DETAIL_MAX`, `EVENT_LISTENER_STREAM_GAP`,
  `EVENT_LISTENER_MISSED_WAKE_WINDOW`. Field types: `UtcTimestamp`, `Checksum`,
  `MacAddress`, `EventCode`, `LivenessState`, `CommandName`.
- **Direction decides strictness (D67).** Outbound (`DesiredConfig`, `Command`) forbids
  unknown fields; inbound (the reported states, `StatusMessage`, `DeviceEvent`) ignores them.
  `schema_version` is top-level only and absent means 1; any other value is rejected.
  Timestamps are aware-only, normalized to UTC, serialized `...Z`. `encode` omits absent
  optionals — but never reaches inside `config`, where a null is data.
  `expected_wake_at` is present exactly while `liveness.state == "sleeping"`, enforced both
  ways. `applied_revision_id` is optional; `command_id` defaults to a fresh UUID.
- **`DesiredConfig.config` IS the `config_revision.snapshot`, verbatim**, and `checksum` is
  that snapshot's D52 checksum. Copying the snapshot through unchanged is load-bearing: it is
  what makes a device-echoed checksum match by construction. The recipe itself stays in
  `app.config.canonical` and is deliberately not imported here (D67).
- **`ContractError(ValueError)`** is the shared base; `TopicError` and `PayloadError` both
  derive from it, so a caller can catch either level. **`PayloadError` is safe to log
  verbatim** — it names the model and the failing fields and never the values (D68).

### `deployment_service` — broker coordinates (E3.1; spec 7.1) — **E5 EXTENDS THIS**

- **Table** (migration `a41f9c7b2e05`, singular per D30): `id`, `deployment_id` FK,
  `service_key` CHECK-constrained to `('mqtt')` for now, `host`, `port` (CHECK 1..65535),
  `tls_enabled`, `ca_cert_pem`, `username`, `password_secret_name`, timestamps.
  UNIQUE `(deployment_id, service_key)`.
- **E5 owns extending it** with the Influx/Grafana/Prometheus/S3 rows, connection tests, and
  the spec 16.5 verification status lifecycle — by widening the `service_key` CHECK and
  adding columns here, **not** by starting a second table. E3 reads this row and writes only
  the `mqtt` one.
- **`deployment_id` IS a real foreign key**, unlike the immutable-evidence tables (D33/D55):
  a service row describes a live connection and is meaningless once its deployment is gone.
- **Credentials never live in the row.** `password_secret_name` names a SecretStore entry
  (`deployment:{deployment_id}:mqtt_password`); `ca_cert_pem` is deliberately NOT a secret —
  it is the public trust anchor the platform verifies the broker against, and storing the
  PEM rather than a path keeps it portable across API replicas and containers.

### The development broker (E3.1; spec 7.1)

- **Generator:** `backend/app/devbroker.py` (`uv run python -m app.devbroker`), documented
  for operators in the README's "Development MQTT broker" section. **Two passes by design:**
  `--certs-only` writes TLS material and empty account files so Mosquitto can start at all;
  the full run needs seeded deployments and writes accounts, ACLs and the service rows.
  `--host` is the hostname the PLATFORM dials (`mosquitto` in compose, `localhost` on the
  host); `--keep-tls` reuses existing certificates.
- **Everything it writes is gitignored** (`deploy/dev-certs/`): private CA, server cert,
  generated passwords, Mosquitto `passwd` (PBKDF2-SHA512, `$7$` format — 2.0 dropped
  plain-text password files) and `acl`, plus `accounts.json`, which is how tests and the
  mock device learn device credentials without a second source of truth. Re-running rotates
  every credential; the password file and SecretStore are rewritten in the same pass so they
  cannot drift apart.
- **The ACL is the isolation guarantee, and it is generated from the topic builders**, not
  from repeated literals: a platform account gets `readwrite eoe/{dep}/#`; an aggregator
  gets exactly seven grants that are spec 7.2's Direction column read literally (read
  `desired`/`cmd`/`lst/+/desired`, write `reported`/`status`/`event`/`lst/+/reported`).
  A device therefore cannot publish its own desired config — which would let it manufacture
  agreement and defeat drift detection.
- **Denial looks like silence.** Mosquitto accepts a subscription to a topic its ACL denies
  (SUBACK 0) and then never delivers, and it filters wildcards per message. Every denial
  assertion in `backend/tests/test_dev_broker.py` is therefore paired with a positive
  control publishing the same retained message to the same topic for an authorized
  identity; a later session must keep that pairing or the suite proves nothing.
- **Compose:** the `mosquitto` service is **TLS only** (8883, no 1883 listener) with
  `persistence true` so retained desired messages survive a broker restart — spec 6.4's
  reconnect property and E3.7's restart acceptance both depend on it.
- **Shared test fixture:** `conftest.ephemeral_broker(dev_dir, host_port=None)` yields a
  `Broker` with `port`, `exec_client(...)`, `reload()`, and (added at E3.2) `stop()`/`start()`.
  It ships files in with `docker cp` rather than a bind mount ON PURPOSE — bind mounts of
  WSL/Windows paths translate differently per host, and the gate must behave identically on
  all three. `host_port` defaults to a Docker-assigned port; pass `conftest.free_port()` only
  when a test must survive a stop/start, because Docker re-assigns port 0 on every start.
- **Gate-locked sets extended here:** `COMPOSE_SERVICES` and `FIXED_PORTS` in
  `backend/tests/test_repo_layout.py` (mosquitto, `8883:8883`), and `E0_TABLES` in
  `backend/tests/test_e0_readiness.py` (`deployment_service`).

### The MQTT client manager (E3.2; spec 7.1, 7.4; D64-D66) — E3.4/E3.5/E3.7 build on this

- **Path:** `backend/app/controlplane/broker.py`, in the package that owns everything talking
  to a broker. The wire contract stays in `app/contracts/mqtt.py`, which is published outside
  this codebase; nothing in `app/controlplane/` is.
- **`BrokerCoordinates`** — `deployment_id`, `slug`, `host`, `port`, `username`, `password`,
  `tls_enabled`, `ca_cert_pem`. The `slug` rides along because it is the `{dep}` topic segment
  (D36) and the manager needs it to build filters. **`password` is `field(repr=False)` and the
  class carries a `__str__` naming only the deployment and the socket** — every log line in
  the module interpolates a coordinates object, so a later edit that adds `%r` still cannot
  leak a credential (rule R2). Keep both.
- **`load_broker_coordinates(session_factory, secret_store)`** reads every `deployment_service`
  row with `service_key == "mqtt"`, ordered by slug, resolving `password_secret_name` through
  SecretStore. A row whose secret is unreadable is **skipped with a warning naming the secret,
  never its value** — one badly provisioned deployment must not deafen the others (D64).
- **`tls_context(coordinates)`** → `ssl.SSLContext | None`. When `ca_cert_pem` is set, that CA
  is the **only** trust anchor (D65) — deliberately not "system store plus this one".
  `check_hostname` and `CERT_REQUIRED` hold on both branches, minimum TLS 1.2; aiomqtt's
  `tls_insecure` is never used.
- **`MqttClientManager(loader, *, backoff, client_id_prefix, keepalive)`** — one asyncio task
  per deployment, each an infinite connect / subscribe / read loop. API:
  `subscribe(filters, handler)` (BEFORE `start()` only — the set is fixed for the manager's
  lifetime, D64), `start()` / `stop()` / `async with`, `publish(deployment_id, topic, payload,
  qos=QOS, retain=False)`, `wait_connected(deployment_id, timeout)`, `is_connected(...)`,
  `deployment_ids`. `filters` has the signature of
  `contracts.mqtt.deployment_subscriptions` — `(slug) -> Sequence[str]` — which is how E3.5
  registers the whole device-to-platform set.
- **`start_or_retry() -> bool`** (E3.7, D87) — what long-lived hosts call instead of `start()`.
  `start()` reads the `deployment_service` rows ONCE and raises if it cannot, and both hosts of
  a manager (the API lifespan, the worker) come up beside Postgres in compose and can beat the
  migrations to that table. Returns True when the connections are open, False when a retry is
  backing off in the background; `stop()` cancels it. **An unreachable BROKER never reaches
  here** — that is the connection loop's own affair. Use `start()` only where a failure should
  propagate, i.e. in tests.
- **The contract with callers: a broker outage is not an event handlers see.** No
  connection-lost callback, no resubscribe hook. Handlers receive `InboundMessage`
  (`deployment_id`, `deployment_slug`, `topic`, RAW `payload` bytes, `qos`, `retain`) and
  nothing else. **A handler that raises is logged and the loop continues** (D64) — one
  device's malformed payload may not cost a deployment its control plane. Payloads stay bytes
  here: decoding is `contracts.mqtt`'s job, and an undecodable payload must still reach the
  handler that decides what to do about it.
- **`BrokerUnavailable`** on publish with no live connection: E3.4 must be able to tell "the
  broker is down" from "published", or a revision moves to `pending` on a lie.
- **Sessions are clean and every connect resubscribes** (D64). The platform does not ask the
  broker to remember its session; delivery guarantees come from QoS 1 plus the retained
  desired topics (spec 6.4). Client identifier: `{prefix}-{slug}-{8 hex}`, one suffix per
  manager instance, so a reconnect retires its own old session but two API replicas never
  collide.
- **Coordinates load once, at `start()`.** Adding a deployment's broker row takes a manager
  restart; E3.7 owns that lifecycle. **Nothing constructs this manager yet** — E3.2 ships the
  library, the worker (D59) wires it up.
- **New runtime dependency: `aiomqtt`** (the phase-3 fixed client choice), which pulls
  `paho-mqtt`. Async tests run on **anyio's pytest plugin** via the `anyio_backend` fixture in
  `conftest.py` — no pytest-asyncio (D66).

### The revision state machine (E3.6; spec 6.2, 14.5; D69-D70) — TEST-CRITICAL

- **Path:** `backend/app/controlplane/revision_state.py`. **`transition()` is the ONLY writer
  of `config_revision.state`** anywhere in the codebase — E3.4, E3.5, E3.7 and E3.10 come
  through it rather than assigning the column, which is what makes the spec 6.2 lifecycle true
  of every row instead of true of the most recently written call site. E2 writes `draft` at
  creation and nothing else (D55).
- **Suite:** `backend/tests/test_revision_state.py`. **One of the four suites no later session
  may weaken (spec 14.5, rule R0.)** It transcribes spec 6.2's transition table verbatim
  (`SPEC_6_2_TABLE`, trigger text included) and the diagram's extra edge separately
  (`SPEC_6_2_DIAGRAM_EXTRA`); the legal set is rebuilt from those transcriptions alone. All 288
  `(source, target, trigger)` triples are enumerated and the 276 outside the legal set must
  raise. **To add a transition you must edit the transcription**, which means reading the spec.
- **`RevisionState`** (StrEnum, compares equal to the stored string): `draft`, `pending`,
  `applied`, `drifted`, `failed`, `superseded`. **`TERMINAL` = {`superseded`} and nothing
  else**; `OPEN` is the other five. Every open state has an exit and every state is reachable
  from `draft` — both suite-asserted, so dead vocabulary cannot creep in.
- **`Trigger`** (StrEnum): `publish`, `report_match`, `report_error`, `timeout`,
  `report_diverged`, `republish`, `retry`, `newer_revision` — spec 6.2's Trigger column, as
  causes. **A transition is a TRIPLE `(source, target, trigger)`, not a pair** (D69): the same
  pair can be legal under one cause and illegal under another, and the trigger is what makes
  `failed` legible on the timeline (rejected the config vs. never answered).
- **`TIMEOUT` attaches to exactly one transition and means silence only** (D70). A device that
  acks a revision with the wrong config fails as `REPORT_ERROR` on its first report; it never
  waits out the window. Suite-pinned — do not widen it.
- **API:** `TRANSITIONS` (frozenset of frozen `Transition` rows carrying `spec_trigger`, the
  spec's own text) · `is_legal` / `legal_targets` / `legal_triggers` · `check(source, target,
  trigger)` raising **`IllegalTransition`** (a plain exception, NOT the API's `AppError` — the
  worker and consumer run outside the HTTP layer; the `ContractError` precedent) ·
  `parse_state` raising **`UnknownRevisionState`** on anything outside the vocabulary (loud
  rather than lenient: treating an unrecognized state as `draft` would republish live config) ·
  `transition(db, revision, target, trigger, *, actor_user_id, detail) -> TransitionRecord` ·
  `load_for_transition` · `open_revisions_for_target` · `supersede_open_revisions`.
- **`check`'s messages distinguish three failures** because they call for different fixes: a
  no-op (a missing idempotency check in the caller), a legal pair under the wrong trigger (the
  right intent reported under the wrong cause, and the message names the permitted triggers),
  and an impossible pair (the message lists where the source can actually go).
- **Stages, never commits** — the `record_audit` convention, so a transition and the publish or
  report that caused it seal or roll back together. `TransitionRecord` (revision id, target,
  deployment, source, target state, trigger, `at`, actor, detail) is what the caller audits
  from; `actor_user_id is None` means system-driven, matching `record_audit`.
- **Concurrency:** `load_for_transition` reads `SELECT ... FOR UPDATE`. An ack and a timeout can
  land on the same pending revision together; the lock serializes them and the guard then
  refuses the loser, so a true `applied` is never overwritten by a `failed` that did not happen.
- **`supersede_open_revisions(db, winner)` closes every OTHER open revision for the winner's
  device, unconditionally — no timestamp comparison.** That is safe ONLY because **E3.4 refuses
  to publish a revision that is not the newest for its device**; the two rules are a pair, and
  removing either alone silently discards drafts (D69).
- **E3.11 hangs `reconciliation_event` off `transition()` itself**, not off each call site —
  one choke point is what will make the timeline complete by construction.

### The desired publish path (E3.4; spec 6.2, 6.4, 7.2, 7.3; D71-D74) — E3.13 wires E2 to this

- **Path:** `backend/app/controlplane/publisher.py`. **`publish_revision` is the ONLY way a
  `config_revision` reaches a device** — steps 1 and 2 of the spec 6.4 loop; E3.5 is the rest.
- **Signature:** `await publish_revision(session_factory, publisher, revision_id, *,
  publish_enabled: bool, actor_user_id: uuid.UUID | None = None) -> PublishOutcome`.
  `publish_enabled` is REQUIRED and keyword-only (D71) — the `EOE_PUBLISH_ENABLED` refusal
  lives inside this function, so no caller can reach a device by forgetting it; the value comes
  from `Settings.publish_enabled` (D61). `publisher` is anything satisfying the structural
  **`DesiredPublisher`** protocol (`async publish(deployment_id, topic, payload, *, qos,
  retain)`) — `MqttClientManager` does, and so does a test double.
- **Owns its own transaction** (D74): a revision can only be published once committed, so
  E3.13's apply commits first and calls this second. **The publish happens INSIDE that
  transaction** — state change staged, bytes sent, commit only on success. A `BrokerUnavailable`
  rolls back and the revision stays `draft` with nothing published. Do not reorder this: a
  committed `pending` nobody was told about resolves as a spec 6.2 `failed(timeout)`, which
  under D70 means "the device never answered" and blames a device for a broker outage.
- **Retained, QoS 1, always** — spec 7.2/6.4's reconnect property, proven by an acceptance test
  that publishes and only THEN connects a subscriber (as the Aggregator's own credential, so
  the spec 7.1 ACL is on trial with it).
- **Payload:** `DesiredConfig` with `config = revision.snapshot` and `checksum =
  revision.checksum`, both VERBATIM (D52). Rewriting, reordering or enriching the snapshot on
  the way out breaks the device-echo match for the whole fleet at once and surfaces as phantom
  drift, not as an error. This module has no secret handling and must never grow any — E2 put
  the markers in the snapshot (spec 5.4, 8).
- **Topics come from LIVE inventory, never `config_revision.deployment_id`** (D71), which is
  historical evidence (D33). Listener revisions resolve their deployment through the
  Aggregator's pod, because the spec 7.2 Listener subtopic hangs off that Aggregator's subtree.
  `resolve_desired_route -> DesiredRoute(deployment_id, slug, aggregator_uuid, device_id,
  topic)` is exported for reuse. A revision whose device has left inventory raises
  **`UnknownPublishTarget`** — expected, not a bug.
- **An aggregator revision's `target_id` is the PLATFORM UUID (`aggregator.id`), NOT the
  `aggregator_uuid`** (D75) — that is what E2 writes. The `{agg}` topic segment and the spec
  7.3 `target.id` are the `aggregator_uuid`; `DesiredRoute.device_id` carries it. Spec 4.2
  keeps the three identifiers distinct and this is the one place that crosses between them.
  Getting it wrong fails SILENTLY — a valid topic no device subscribes to — so any future
  consumer of `target_id` must resolve, never string-copy. Listener `target_id` is the MAC,
  which is both.
- **The trigger follows the source state** (D71): `draft` → `publish`, `drifted` → `republish`,
  `failed` → `retry`, all reaching `pending` via `transition()`. New states must be added to
  `_TRIGGER_FOR` deliberately; it does not default.
- **Idempotent republish** (D72): a `pending` or `applied` revision re-sends byte-identical
  retained bytes, moves NO state and writes NO second audit row (`PublishOutcome.transitioned`
  is False). It re-sends rather than no-oping because that is the repair for a broker that lost
  its retained store.
- **Only the newest revision for a device may be published** (D73), by `(created_at, id)`;
  otherwise **`StaleRevision`**, as for a `superseded` one. **This is the pair rule that makes
  `supersede_open_revisions` safe** — see the state-machine section above; removing either half
  alone silently discards an operator's newer draft.
- **Exceptions** all derive from **`PublishError`** (a plain exception, not `AppError` — the
  worker and E3.13's apply call this outside the HTTP layer; the `ContractError` precedent):
  `PublishDisabled`, `UnknownRevision`, `UnknownPublishTarget`, `StaleRevision`. The API
  boundary translates.
- **Audit:** one `revision.publish` row per state-moving publish (`AUDIT_ACTION`), entity
  `config_revision`, scoped to the resolved deployment, detail carrying target, topic, checksum,
  from/to state, trigger and the superseded revision ids. `actor_user_id is None` means
  system-driven, per the `record_audit` convention.
- **Suite:** `backend/tests/test_publish_revision.py` (gate 43).

### The reported consumer (E3.5; spec 4.3, 6.1, 6.2, 7.3, 7.4; D76-D79) — E3.7 runs it

- **Path:** `backend/app/controlplane/consumer.py`. The second half of the spec 6.4 loop:
  E3.4 publishes and stops, this consumes reported Aggregator state, reported Listener
  state and events. A LIBRARY like the client manager — **nothing constructs it yet**;
  E3.7's worker wires the two with `manager.subscribe(consumer.filters, consumer.handle)`.
- **API:** `ReportedConsumer(session_factory)` · `filters(slug)` (returns
  `deployment_subscriptions(slug)` verbatim — one namespace, not a second list) ·
  `async handle(message: InboundMessage) -> ReportOutcome` (satisfies
  `broker.MessageHandler`; runs the sync ORM work in a worker thread) · `consume(message)`,
  its synchronous body, which is what the suite drives. Helpers: `latest_state(db,
  entity_type, entity_id)`, `delete_device_state_for(db, entity_type, entity_id)`,
  `differing_keys(reported, desired)`. Constants: `AUDIT_ACTION = "revision.report"`,
  `QUARANTINE_UNKNOWN_MAC`, `MAX_DIFFERING_KEYS = 20`.
- **`ReportOutcome`** (StrEnum, returned per message so E3.7 can count without re-deriving):
  `applied` · `rejected` · `drifted` · `unchanged` · `stale` · `quarantined` ·
  `unprovisioned` · `malformed` · `event` · `duplicate_event` · `misrouted` · `not_mine`.
- **Identity comes from the TOPIC, never a payload** (D79). The spec 7.1 ACL authenticated
  the `{agg}`/`{mac}` segments; a payload field would be a self-declaration. No inbound
  spec 7.3 model carries an identity field and none should grow one.
- **Identity routes through the E1.5 services, which never touch inventory.** MAC/name
  conflicts quarantine and alert; an unknown `aggregator_uuid` opens `provisioning_required`
  on the reported AND event channels; an unregistered MAC quarantines with reason
  `unknown_mac` and NO alert (D76). **A report that fails an identity check writes no
  `device_state` row and moves no revision** — not just no inventory row.
- **The spec 6.2 edges a report can drive:** `pending`+match → `applied` (`report_match`);
  `pending`+coherent mismatch → `failed` (`report_error`) **immediately**, never waiting out
  the window (D70); `applied`+mismatch → `drifted` (`report_diverged`). From `draft`,
  `drifted`, `failed` and `superseded` a report moves NOTHING — checked BEFORE the machine is
  asked, since offering it an illegal triple would raise where nothing is wrong. All through
  `transition()`, still the only writer of `config_revision.state`.
- **Two boundary refusals with NO transition** (D70), because a malformed message is not
  evidence about whether config was applied: an undecodable payload, and a device whose
  `checksum` is not `config_checksum(config)` — the platform recomputes rather than trusting
  a naked field, which is what makes the D52 echo-match a property and not a hope.
- **Mismatch detail names differing KEYS, never values** (rule R2) — bounded at
  `MAX_DIFFERING_KEYS`, since it lands in an audit row and on the E3.11 timeline.
- **Staleness** (D78): a report strictly older than the stored one is dropped WHOLE.
  Equal-timestamp replays run the full comparison and land on "already there", so
  idempotency is a property of `applied_revision_id` plus checksum as spec 7.4 words it.
  `stale` therefore means late delivery and nothing else.
- **Audit:** one `revision.report` row per state-moving report, entity `config_revision`,
  `actor_user_id is None` (a device said so, not a user), detail carrying target, from/to
  state, trigger, `reported_at`, the reported checksum and any `differing_keys`. No row for a
  report that changed nothing — reports arrive continuously and the audit trail records what
  CHANGED (the D72 reasoning).
- **Suite:** `backend/tests/test_reported_consumer.py` (gate 44), including the three phase
  acceptance criteria and one live-Mosquitto test that publishes as the device's own
  credential through the real manager — the only test that proves `filters` and `handle` are
  wired, since every other one hands the consumer a message the suite built.

### `device_state` and `device_event` (E3.5; spec 6.1, 7.3; D77-D78) — **E3.8/E3.9 EXTEND
`device_state`**

- **`device_state`** (migration `a2cf00fc037f`): `id` · `entity_type` CHECK
  `('aggregator','listener')` · `entity_id` — **the `config_revision` convention exactly**,
  aggregator PLATFORM UUID / listener MAC (D75), un-FK'd for the `entity_override` reason ·
  `deployment_id` **FK** (current state, not evidence — the `deployment_service` precedent,
  not D33) · `reported_at` (the device's clock; the spec 7.4 ordering key) ·
  `applied_revision_id` nullable, un-FK'd · `checksum` · `config` JSONB (markers, never
  plaintext) · `health` JSONB nullable (stored as sent, **never charted** — Prometheus is
  authoritative, spec 10.1) · `received_at`. UNIQUE `(entity_type, entity_id)`: one row per
  device, replaced in place.
- **E3.8 adds the LWT online state** spec 9.3 makes authoritative for an Aggregator's live
  verdict; **E3.9 adds the spec 6.5 Listener liveness block**. E3.5 stores neither on
  purpose. Add columns here, not a second table.
- **Deleted with its device:** `delete_device_state_for` is wired into the E1 aggregator and
  listener DELETE endpoints (the `delete_overrides_for` precedent, D51), so a MAC reused by
  a different physical device cannot inherit its predecessor's reported config.
- **`device_event`**: `id` · `deployment_id` un-FK'd indexed · `aggregator_uuid` (the EMITTER
  the broker authenticated) · `listener_mac` nullable indexed · `at` · `level` CHECK
  `('debug','info','warn','error')` · `code` · `detail` · `received_at`. Immutable evidence
  (D33): it outlives the device it describes and has no cleanup hook.
- **`uq_device_event_delivery` UNIQUE `(deployment_id, aggregator_uuid, listener_mac, at,
  code)` `NULLS NOT DISTINCT`** (D77): a QoS 1 redelivery is a no-op, not a second timeline
  entry. The NULLS clause is load-bearing — without it only Listener events would dedupe.
  Requires Postgres 15+. The same code at a different instant is a different event.
  `ix_device_event_timeline (aggregator_uuid, at)` pre-pays E3.11's query.

### The reconciliation worker (E3.7; spec 6.4, 14.3; D80-D81, D84-D86) — E3.8-E3.12 EXTEND IT

- **Path:** `backend/app/controlplane/runner.py`. The process that runs the spec 6.4 loop:
  it owns an `MqttClientManager` with E3.5's `ReportedConsumer` registered on it, and
  schedules the two things no message-driven code could do.
- **Two entrypoints, one module (D59):** `python -m app.controlplane.runner` (the compose
  `worker` service) and the API lifespan under `EOE_WORKER_IN_API` (default off).
- **API:** `ReconciliationWorker(session_factory, secret_store, *, timeout_interval=30.0,
  drift_interval=300.0, manager=None, consumer=None)` · `async start()` / `stop()` /
  `__aenter__` / `wait_closed()` · `.manager` (the live client manager — E3.13 and the API
  publish through one) · `.counters` (diagnostics only; all real state is in Postgres).
- **`pending_timeout_sweep(session_factory, *, now=None) -> TimeoutSweepReport`** (spec 6.4
  item 4): `pending` revisions whose deployment window has elapsed go
  `failed(timeout)`, with a `revision.timeout` audit row and `actor_user_id is None`. Report
  carries `failed` · `overtaken` (an ack won the race under the row lock) · `unanchored`.
- **`drift_sweep(session_factory) -> DriftSweepReport`** (spec 6.4 item 5): stored
  `device_state` vs the applied revision → `drifted(report_diverged)` plus a
  `revision.drift` audit row (detail names differing KEYS, never values); recomputed
  effective config vs that revision → `desired_changed`, an **observation with no
  transition** (D85). Report carries `drifted` · `desired_changed` · `unresolvable` ·
  `unreported` · `auto_reconcile_requested`.
- **The sweeps are module-level functions** taking a session factory: the suite drives them
  with no event loop, no broker and no worker. One transaction per revision; the scan is
  lockless and every transition re-reads under `load_for_transition` (D80).
- **Nothing here republishes.** `deployment.auto_reconcile` is stored, default off, INERT
  pending spec 17 item 3 (D81) — the worker reads it only to report it. Drift is repaired
  by `POST /revisions/{id}/publish`.
- **Suite:** `backend/tests/test_reconciliation_worker.py` (gate 45), including both halves
  of the phase acceptance against a real broker: the full journey (publish → ack → drift →
  operator re-publish → timeout) and a worker restart mid-flight.

### Reconciliation policy and the window anchor (E3.7; D81, D84) — **E7 MAY EXTEND**

- **`deployment.pending_timeout_seconds`** (migration `c41e9b7d3a58`): int, default 300,
  CHECK > 0. The spec 6.4 item 4 window, per deployment. A PLATFORM setting — never a
  catalog key, because a catalog key would be merged into effective config and published to
  devices. A revision whose deployment row is gone falls back to
  `models.DEFAULT_PENDING_TIMEOUT_SECONDS` (300) rather than sitting `pending` forever.
- **`deployment.auto_reconcile`**: bool, default false, **inert** (D81). The phase that
  implements the spec 17 item 3 policy is the phase that may act on it.
- **`config_revision.published_at`** (same migration): nullable timestamptz, indexed,
  written by `revision_state.transition` on EVERY edge into `pending` and by nothing else
  (D84). **This extends an E2-owned table**: E2 owns the row up to `draft`, E3 owns every
  state after it. Not backfilled; a `pending` row without it is reported, never timed out.

### `POST /revisions/{revision_id}/publish` (E3.7; spec 6.2, 6.4; D82-D83, D86)

- **The operator half of the loop**, on the E2.6 revisions router. CSRF; two-step scoping —
  `VIEW_STATUS` decides 404 (the D35 existence-oracle rule), `MANAGE_CONFIG` decides 403.
  Calls E3.4's `publish_revision` and adds no publish logic of its own.
- **Response:** `{revision_id, topic, deployment_id, checksum, state, trigger,
  transitioned, superseded[]}`. `transitioned: false` is the idempotent re-publish (D72).
- **Refusals:** publication disabled → 409 · stale/superseded/device-gone → 409 · broker
  down → **503 `service_unavailable`** (the D8 vocabulary's seventh code, D83) · revision
  vanished → 404. Nothing is half-done on any of them (D74).
- **`app.state.mqtt`** is the API's own publish-only manager, started by the lifespan **only
  when `EOE_PUBLISH_ENABLED` is on**, and None otherwise (D86). It registers no
  subscriptions, so it never consumes what the worker consumes. E3.13 publishes E2's apply
  through the same attribute.
- **Compose:** the `worker` service (no ports, `python -m app.controlplane.runner`) joins
  `COMPOSE_SERVICES` in `backend/tests/test_repo_layout.py`, the same deliberate-extension
  discipline as `E0_TABLES`.
