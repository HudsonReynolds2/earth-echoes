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
  `totp:{user_id}`, `deployment:{id}:{service_key}`, `bundle:{id}:{key}`.
- **Guarantees (tested):** plaintext never in the database, logs, or error messages; GCM
  authentication rejects tampering; a KEK mismatch fails loudly with fingerprints, not
  values.
- **Consumers:** E4 (device-facing bundle secrets held before the separate spec-8.4
  firmware encryption is applied at export — the two schemes nest, they do not compete),
  E5 (deployment service credentials), E0.10 (TOTP secrets).

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
  `provisioning_required` alert; use it on metrics/analysis/object ingest paths.
- **Tables:** `quarantined_report` (append-only evidence; no FK to listener — survives
  deletion, holds reports about devices inventory never had) and `inventory_alert`
  (open alerts unique per (alert_type, entity_type, entity_key) via partial unique index
  `WHERE resolved_at IS NULL`; `deployment_id` is un-FK'd scope; `resolved_at` NULL =
  open). Alert types are data, not D8 wire codes. E7 unifies alert surfacing later.
