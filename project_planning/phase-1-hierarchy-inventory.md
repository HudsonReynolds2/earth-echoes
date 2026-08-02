# Phase 1 Document: Hierarchy and Inventory (Epic E1)

**Companion documents:** Technical Specification v1.1 (authoritative), Project Development Plan v1.0
**Spec sections implemented:** 4, 13 (hierarchy surface)
**Depends on:** E0 complete.

---

## 1. Scope

Build the Organization to Listener data model, full CRUD with the spec's validation rules, bulk import, duplicate-identifier handling, tags, and the inventory UI. When this phase ends, an operator can model an entire real deployment in the platform before any other subsystem exists.

## 2. Prerequisites and inherited interfaces

From E0 (read `docs/INTERFACES.md` before writing code): the API conventions and error envelope, the RBAC permission dependency (apply it to every endpoint here; writes require `deployment_operator` or above within scope, reads allow `viewer`), the audit hook (call it on every mutation), the migration conventions, and the frontend token structure and layout shell.

Fixed choices for this phase:

- **Identity.** Organizations, Deployments, Pods, and Aggregators use UUIDv4 primary keys. Listeners key by MAC address (spec 4.2): normalized to uppercase colon-separated form (`AA:BB:CC:DD:EE:FF`), validated by regex on input, stored normalized.
- **Deployment slug.** Deployments carry a unique `slug` (lowercase, URL-safe, generated from the name, editable before first use) because the MQTT topic namespace keys on it (spec 7.2, `{dep}`). E3 consumes this; get it right now.
- **Aggregator identity columns.** `id` (platform UUID), `aggregator_uuid` (first-class, unique within the Organization, indexed; spec 4.2), `balena_uuid` (nullable). Do not conflate them.
- **Hierarchy foreign keys.** Deployment → organization_id; Pod → deployment_id; Aggregator → pod_id with a uniqueness constraint enforcing exactly one Aggregator per Pod (spec 13); Listener → aggregator_id.
- **Deletion semantics.** DELETE rejects when children exist (409 with the error envelope). No cascade deletes in v1.
- **Names.** Listener names unique within their Deployment (enforced by constraint plus application check for the auto-suffix flow). Pod and Deployment names unique within their parent (spec 4.2).

## 3. Out of scope

Configuration overrides, the settings catalog, and effective config (E2). Anything MQTT, including acting on live reported messages: task E1.5 builds the handling logic as callable services, and E3 wires them to the broker. Provisioning bundles and `settings.yaml` (E4). Map rendering (E6): the inventory UI here is trees and tables only. GPS fill-in UX beyond plain fields on the Listener form (the guided flow is E4.11).

## 4. Task list

**E1.1 Hierarchy schema.** Migrations for `organizations`, `deployments`, `pods`, `aggregators`, `listeners` with the columns, keys, and constraints above, plus `location.gps_lat`/`gps_lon` nullable on Listener (spec 5.3 reserves them; store them as inventory columns, not config overrides) and timestamps throughout. Acceptance: constraints proven by tests (duplicate MAC insert fails, second Aggregator on a Pod fails, duplicate Listener name within a Deployment fails, same name across Deployments succeeds).

**E1.2 CRUD endpoints.** The spec 13 hierarchy surface: `GET/POST` collections and `GET/PATCH/DELETE` items for all five entities, Listeners addressed by MAC. Pagination, filtering (by parent, name substring, tag), and sorting on all lists. RBAC scoping: non-owner users see only entities under their assigned Deployments. Acceptance: endpoint tests per entity including scope filtering; deletion-with-children returns 409.

**E1.3 One Aggregator per Pod.** Pod creation either creates or attaches its single Aggregator in one call; attaching to an occupied Pod returns 409 (spec 13). Acceptance: both create-and-attach paths tested.

**E1.4 Uniqueness validation and auto-suffix.** On create and import: Listener name collision within a Deployment rejects by default and offers auto-suffix (`name`, `name-2`); MAC collision always rejects (spec 4.3 item 1). `aggregator_uuid` uniqueness validates within the Organization (spec 4.3 item 3). Acceptance: the auto-suffix option is an explicit request parameter, never silent; tests cover reject and suffix paths.

**E1.5 Duplicate identity handling at report time (logic only).** A service layer, callable without MQTT, implementing spec 4.3 items 2 and 3: match reports by MAC; on name disagreement or two devices reporting one MAC, raise a `duplicate_identity` condition and write the conflicting report to a `quarantined_reports` table instead of touching inventory; on an unknown `aggregator_uuid`, raise `provisioning_required` via the membership check (never equality to a sentinel). Alerts here are rows in a simple `inventory_alerts` table this phase owns; E7 later unifies alert surfacing. Acceptance: table-driven tests for clean match, name conflict, MAC conflict, and unknown aggregator; inventory rows provably untouched in conflict cases.

**E1.6 Bulk import.** CSV and JSON import endpoints for Listeners and Aggregators with per-row validation results (row number, status, error detail), applying the E1.4 rules, all-or-nothing per request by default with a partial-accept option. Document the CSV column format in `docs/INTERFACES.md`. Acceptance: an import mixing valid and invalid rows returns row-level results; partial-accept commits only valid rows.

**E1.7 Tags.** Free-form string tags on every entity, `GET/PUT /{entity}/{id}/tags`, list-endpoint filtering by tag. E2's selection engine builds on these; keep storage simple (a tags array or join table, indexed for filtering). Acceptance: tag set/replace semantics tested; filter-by-tag works on every entity list.

**E1.8 Inventory UI.** A hierarchy tree for navigation, TanStack Table device tables per level with the list filters, create/edit forms per entity (including the auto-suffix prompt on name collision), tag editing, and a bulk import screen showing per-row results. All styling through E0 tokens. Acceptance: a user can build a full Organization → Listener hierarchy and run an import entirely in the UI; role gating hides writes from viewers.

**E1.9 Demo fixture.** Extend the E0 seed with a realistic demo hierarchy (1 org, 2 deployments, a few pods each, listeners at varied counts) used by tests and later phases. Acceptance: one command seeds it; E2 and E6 sessions can rely on it by name.

> **Addendum PHASE1-4-01 (2026-08-02, ref project-changes #13):** E1.2's "`GET/PATCH/DELETE` items for all five entities" loses to spec 13, which lists no DELETE for `/organizations` (consistent with spec 12.1's single-organization v1). The shipped surface is `GET/POST /organizations` and `GET/PATCH /organizations/{id}`; the other four entities carry DELETE with the 409-on-children rule unchanged. DECISIONS D34.

> **Addendum PHASE1-4-03 (2026-08-02, ref project-changes #15):** E1.5's "raise a condition" ships as return-based resolutions: `handle_reported_identity` returns an `IdentityResolution` (MATCHED / NAME_CONFLICT / MAC_CONFLICT / PROVISIONING_REQUIRED / UNKNOWN_MAC) and writes the specified `quarantined_reports`→`quarantined_report` and `inventory_alerts`→`inventory_alert` rows (singular table names per D30); `require_known_aggregator` is the raising variant for ingest paths. Every acceptance case holds as written. DECISIONS D37.

> **Addendum PHASE1-4-02 (2026-08-02, ref project-changes #14):** `POST /organizations` returns 409 `conflict` while an organization already exists — v1 is single-org (spec 12.1), and the clamp keeps the D32 reasoning honest (global `aggregator_uuid` uniqueness equals within-org uniqueness only while one org exists). Multi-org later removes the clamp and relaxes that constraint in the same change. DECISIONS D34.

## 5. Definition of done

An operator creates the full hierarchy in the UI and by API, bulk-imports Listeners from CSV with row-level validation results, and edits tags. All spec 4.2/4.3 identity rules hold under test, including the report-time logic with quarantine. RBAC scopes every endpoint; every mutation audits. The demo fixture seeds cleanly. Migrations upgrade and downgrade.

## 6. Handoff artifacts

- `docs/INTERFACES.md` updated with: the entity schema summary (tables, keys, constraints), the deployment slug rule, MAC normalization, the CSV import format, the E1.5 service interfaces E3 will call (`handle_reported_identity(...)`, `check_aggregator_membership(...)` or equivalent names as built), and the tag storage model E2 queries.
- The demo fixture name and contents documented.
- `docs/DECISIONS.md` updated with any deviations.
