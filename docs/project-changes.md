# Project Changes

Numbered, reverse-chronological, append-only log of changes to scope, sequencing, task
definitions, or acceptance criteria relative to the planning documents (rule R1,
`.claude/rules/project-rules.json`). Every entry names an addendum that exists in the
referenced planning document; an entry with no addendum is incomplete.

## #21 (2026-08-10): Dev host ports moved into the 1xxxx range

- **What changed:** the published HOST ports of the dev compose stack move from
  8000/5173/5432/6379 to **18000/15173/15432/16379**, and E3.1's new broker publishes
  **18883** rather than 8883. Phase-0 section 2 fixes the old numbers, so this amends it.
- **What did NOT change:** the container side. Every service still listens on its standard
  port inside the network (8000/5173/5432/6379/8883), so no image, no uvicorn or vite
  argument, no `mosquitto.conf` listener and no service-to-service URL moved — only the
  host mapping and the host-facing URLs (`EOE_CORS_ORIGINS`, `VITE_API_BASE_URL`, the
  verifier's default `--api`, the QA script's probes, and the guides).
- **Why:** the standard ports collide with other services commonly running on a developer
  machine, and the collision is not cosmetic — it makes the gate impossible to pass, since
  `test_compose_stack` and `test_verify_tool` bind them for real (rule R0 admits no
  skipping). Owner instruction, 2026-08-10.
- **Enforcement:** `FIXED_PORTS` in `backend/tests/test_repo_layout.py` remains the single
  enforced list and now carries HOST:CONTAINER pairs; `docs/INTERFACES.md` records the new
  host ports beside a pointer to it.
- **Affects:** project_planning/phase-0-foundations.md §2 (ports)
- **Addendum:** PHASE0-2-02

## #20 (2026-08-10): E3 sequencing — contracts module lands at E3.1, state machine before publish

- **What changed:** two ordering changes inside epic E3, both within their own batch.
  (1) `backend/app/contracts/mqtt.py` is created by **E3.1** with the spec 7.2 topic
  builders instead of arriving whole at E3.3; E3.3 adds the spec 7.3 payload models to it.
  (2) Batch 2 runs **E3.6 (state machine) before E3.4 (publish) and E3.5 (consumer)**,
  instead of the phase document's 3.4 → 3.5 → 3.6 order.
- **Why:** (1) E3.1's broker ACL grants ARE topic strings; generating them from literals
  and refactoring two tasks later would put the topic namespace in two places, which the
  "single contracts module" fixed choice exists to prevent (DECISIONS D62). (2) Both the
  publisher and the consumer transition revision state, so the module that owns transitions
  must exist before either calls it rather than open-code a state write.
- **Who approved:** the owner approved the batch-2 reorder with the E3 plan on 2026-08-10;
  the handbook (section 2) allows a session to propose a task order.
- **Effect on scope:** none. All thirteen E3 tasks still ship, each with its own gate.
- **Affects:** project_planning/phase-3-control-plane.md §4 (tasks E3.1, E3.3-E3.6)
- **Addendum:** PHASE3-4-01

## #19 (2026-08-10): E3's E2-coordination contingency is moot; E3.13 is in scope

- **What changed:** the phase-3 document was written expecting E3 to run in parallel with
  E2 and hedges accordingly — agree the `config_revision` shape with the E2 session, fall
  back to consuming revision snapshots if the merge engine has not landed, and treat E3.13
  as conditional on E2 merging. E1 and E2 both completed before this phase started (gates
  20-38, PRs #9-#16), so none of those contingencies apply.
- **Why:** the epics ran in sequence, not in parallel. `config_revision` arrives exactly as
  E2 defined it (D55); `app/config/service.py::effective_resolved` is available to the
  publisher and the drift re-compare from the day E3.7 lands; and E3.13 — wiring apply to
  `publish_revision` and flipping `EOE_PUBLISH_ENABLED` on — is ordinary in-scope work for
  this phase rather than a deferred follow-up.
- **Owner decisions taken at plan approval (2026-08-10):** DECISIONS D59 (worker topology
  and the Postgres LISTEN/NOTIFY websocket bus), D60 (E3 lifts D40 on real reported state),
  D61 (`EOE_PUBLISH_ENABLED` defaults on at E3.13).
- **Effect on scope:** none removed; E3.13 becomes unconditional.
- **Affects:** project_planning/phase-3-control-plane.md §2 (prerequisites and coordination)
- **Addendum:** PHASE3-2-01

## #17 (2026-08-04): E2.2's level rule inverted to match spec 5.3 — at-or-above, never below

- **What changed:** task E2.2 says "writing a key below its allowed level is permitted
  per spec 5.3's inheritance note". The shipped validator enforces the OPPOSITE: a key
  may be overridden at its lowest level or at any ancestor level, never below
  (`network.wifi_ssid`, lowest level Pod, is settable at organization/deployment/pod and
  rejected at aggregator/listener; `lowest_level='any'` is settable everywhere).
- **Why:** the phase-doc sentence misreads the spec. Spec 5.3's actual inheritance note
  says "inheritance permits setting most keys **higher**" than their lowest level, and
  spec 5.1's rationale argues directly against below-level writes (network keys resolve
  at Pod because every Listener in the pod shares one network — a per-listener WiFi
  password contradicts the model). Authority order: spec beats phase doc. Owner decided
  2026-08-04 during E2 planning (the same resolution shape as #13's org-DELETE).
- **Affects:** project_planning/phase-2-configuration-model.md §4 (task E2.2)
- **Addendum:** PHASE2-4-01

## #18 (2026-08-04): E2.6 concretized — write-at-level, per-device draft revisions, revisions routes assigned

- **What changed:** task E2.6's sketch left three things open that the shipped design
  fixes: (a) apply gains an explicit **write-at-level** control — `level="target"`
  (default) writes onto each matched entity, a named level writes once at the single
  common ancestor or 422s on a split; (b) revisions are **per-device only**
  (aggregator/listener, per spec 6.1/7.2's topic addressees) with marker-bearing
  snapshots composed per spec 5.4, and the phase doc's "target entity type and id"
  sketch is narrowed accordingly; (c) the spec-13 **revisions read routes**
  (GET /{device}/{id}/revisions, GET /revisions/{id}), which no phase-2 task claimed,
  land in E2.6 because E2.8's acceptance ("sees draft revisions listed") needs them.
  Apply stops at draft unconditionally; `EOE_PUBLISH_ENABLED` exists, defaults off, and
  is only reported until E3's publisher consumes it.
- **Why:** spec 6.1 defines desired config per Aggregator/Listener; a selection is not
  a topic addressee. The common-ancestor rule keeps a bulk edit one auditable write
  instead of N copies. Owner decisions of 2026-08-04 (the E2 plan).
- **Affects:** project_planning/phase-2-configuration-model.md §4 (task E2.6)
- **Addendum:** PHASE2-4-02

## #16 (2026-08-02): Overview ships a minimal real roll-up ahead of E6/E7

- **What changed:** the Overview page ("/") is rebuilt with the V2·S1 layout carrying
  ONLY E1-owned data — a serif hero of **listeners registered** (honest label, not
  "online"), a real meta line, one card per deployment with real pod/listener counts —
  while every slot whose data belongs to a later epic stays an honest EmptyState naming
  it (attention feed → E3/E7; status/services lines → E3/E5; no telemetry volume). The
  page title becomes "Organization overview" (V2·S1's title; v2 wins on values), which
  retitles one shell.test row and two auth.test assertions (recorded in D42). E1's task
  list does not include Overview; building the E1-ownable half now mirrors how DES.7
  shipped page skeletons ahead of their epics (#9's precedent), with the owner's explicit
  decision (2026-08-02).
- **Guard:** the no-fabricated-status rule (D40) is asserted by test on this page.
- **Affects:** project_planning/phase-1-hierarchy-inventory.md
- **Addendum:** PHASE1-4-04

## #15 (2026-08-02): E1.5's "raise a condition" implemented as return-based resolutions

- **What changed:** task E1.5 says the service should "raise a `duplicate_identity`
  condition" / "raise `provisioning_required`". The shipped design returns an
  `IdentityResolution` with an outcome enum instead of raising for expected conditions,
  writes the alert rows the task specifies, and offers `require_known_aggregator` as a
  raising variant. Recorded so the wording difference is a documented choice, not drift:
  E3.5's consumer loop handles every outcome uniformly, and exception control flow for
  expected message states would push try/except into every ingest path.
- **Why:** the task's own acceptance tests are outcome-shaped (clean match, name
  conflict, MAC conflict, unknown aggregator — all verified); the "condition" language
  named the states, not the mechanism.
- **Affects:** project_planning/phase-1-hierarchy-inventory.md
- **Addendum:** PHASE1-4-03

## #14 (2026-08-02): POST /organizations clamped to a single organization

- **What changed:** creating a second organization returns 409 `conflict` while v1 runs
  single-org. Spec 12.1 fixes v1 as one Organization whose scoping flows through joins;
  an unclamped POST would let two orgs exist with no scoping story and would falsify the
  global-uniqueness-equals-within-org reasoning D32 records for `aggregator_uuid`.
  Multi-org later removes the clamp and relaxes that constraint together (D32/D34
  cross-reference).
- **Why it is a plan change:** E1.2's task text implies plain POST semantics for all five
  entities; the clamp narrows one of them deliberately.
- **Affects:** project_planning/phase-1-hierarchy-inventory.md
- **Addendum:** PHASE1-4-02

## #13 (2026-08-02): Organizations get no DELETE endpoint

- **What changed:** the E1.2 surface ships `GET/POST` collection and `GET/PATCH` item
  routes for `/organizations` — no DELETE. Task E1.2's wording ("`GET/PATCH/DELETE` items
  for all five entities") conflicted with spec 13, which lists no DELETE for
  organizations, consistent with the single-org v1 of spec 12.1. The spec is first in the
  authority order; the owner confirmed the resolution (2026-08-02) before implementation.
- **Affects:** project_planning/phase-1-hierarchy-inventory.md
- **Addendum:** PHASE1-4-01

## #12 (2026-08-01): Records hygiene batch — the DES-batch paper trail brought current

- **What changed:** No product code. A records-and-test-hygiene pass closing the gaps a
  post-DES review of `23eff5d..f93f061` found: (1) `project_planning/phase-0-foundations.md`
  gains PHASE0-4-05 (D25's shell replacement, previously recorded everywhere except the phase
  document) and PHASE0-4-06 (correcting PHASE0-4-04's claim that the E0.4 acceptance criteria
  "continue to hold unchanged" — the one-file token-sheet criterion stopped holding literally
  at the same gate that appended that addendum). (2) Entry #11 below gives Gate 17's backdrop
  change the numbered record it never had, and records that #9 was amended in place. (3)
  DECISIONS D28 records the fifth Gate 16 test change D26's "all four" inventory missed;
  D29 records this batch's two test strengthenings (`test_governance.py` non-empty baseline
  guard; `users-admin.test.tsx` renamed and made to assert D25's actual nav intent). (4) Stale
  statements corrected in `DES.4-handoff.md` (claimed `Screens.dc.html` "isn't in this repo" —
  it landed at Gate 16) and `DES-track-handoff.md` (claimed to be a frozen 2026-07-30 snapshot
  while its table was updated through Gate 18). (5) `docs/frontend-guide.md` is subordinated to
  `docs/INTERFACES.md` "Frontend composition and shared components" with a precedence note and
  gains an owner-supplied "Starting E1" brief; INTERFACES.md cross-links back. (6)
  `frontend/public/images/README.md` gains an explicit provenance/license TODO for
  `forest-background.jpg` so the one compliance gap the DES batch left is tracked, not silent.
- **Why:** Rule R1 exists so a fresh session can trust the documents in the authority order.
  The phase doc contradicted the shipped shell and token reality, the change log had been
  edited in place, and one test's name asserted an invariant D25 deliberately abandoned.
- **Affects:** project_planning/phase-0-foundations.md
- **Addendum:** PHASE0-4-06

## #11 (2026-08-01): Late numbered record — Gate 17's backdrop asset change, and the in-place amendment of #9

- **What changed (at Gate 17, 2026-07-30, commit `7d5400a`):** `forest-background.jpg`
  committed web-sized (1920×1280, ~925 KB, EXIF stripped; the supplied original was a 24 MB
  camera file) and wired to back **every** page via `.shell-content`, scrimmed by the new
  additive token `--eoe-color-backdrop-scrim` (0.93 light / 0.94 dark, on D21's terms); the
  login hero keeps the lighter `--eoe-color-overlay` treatment. Both treatments paint a
  `background-color` first so a missing file degrades to a flat token color.
- **Why this entry is late:** at Gate 17 the change was recorded by amending entry #9 in
  place ("#9, amended" in the Gate 17 update) instead of appending a numbered entry. This log
  is append-only; a new token, a permanently committed binary, and an all-page visual
  treatment constitute a plan change that warrants its own number. #9's amended text stands
  as-is — rewriting it again would repeat the mistake — and this entry plus addendum DES-7-03
  are the durable record.
- **Affects:** project_planning/DES-track-handoff.md
- **Addendum:** DES-7-03

## #10 (2026-07-31): Fonts vendored and the DES track handed off to E1

- **What changed:** The DES track's last open implementation item closes before E1 starts.
  (1) **Fonts are vendored** rather than named-and-hoped: seven latin-subset woff2 files in
  `frontend/public/fonts/` (~160 KB) declared by the new `frontend/src/styles/fonts.css`, with
  `frontend/tests/fonts.test.ts` gate-enforcing that nothing is fetched from a CDN, that every
  family a token names is actually supplied, and that the status glyph subset covers every
  glyph token. (2) The seventh file exists because of a finding: **none of the six status
  glyphs exists in IBM Plex Sans, IBM Plex Mono, or Source Serif 4** (checked against the
  complete families), so the shape channel — one of the three channels the status vocabulary
  requires — was resolving to system fallback and to tofu on a minimal air-gapped host. It now
  ships as a 568-byte Noto Sans Symbols 2 subset behind the additive token
  `--eoe-font-family-glyph` (DECISIONS D27). (3) `docs/INTERFACES.md` gains **"Frontend
  composition and shared components"**: the props and use of `PageHeader`, `ContextBar`,
  `StatusChip`/`StatusLegend`, `EmptyState`, and `Can`, the two page-composition shapes, and
  the conventions later epics must follow (no literals, component CSS in `app.css`, serif is
  display-only, mono for identifiers, density from the row/control-height tokens, `.admin-table`
  as the pattern E1.8's TanStack tables generalise).
- **Why:** E1.8 builds a hierarchy tree, device tables, entity forms, and a bulk-import result
  grid — the first heavy consumer of the design system. An implementation session's inputs are
  the spec, its phase document, `docs/INTERFACES.md`, and `docs/DECISIONS.md`; the component
  library those sessions need was documented only in `Design System.dc.html`, which is **not in
  this repository**. Without this, E1 would re-invent table, form, and status vocabulary and the
  direction would drift on its first real use. Vendoring the fonts now also settles the type
  metrics before dense tables are built against them.
- **Also recorded:** open question 3 (config editor on tablet) stays open and belongs to E2, not
  E1. DES.8 remains blocked on E4–E6.
- **Affects:** project_planning/DES-track-handoff.md ("The three rules" item 3, status table,
  open questions)
- **Addendum:** DES-7-02

## #9 (2026-07-30): DES.7 applied — shell restructured, night theme shipped, page skeletons ahead of their epics

- **What changed:** DES.7 ("apply the design system") is done for the shell and the frame.
  `Shell.tsx` becomes V2·S1's dark top bar (DECISIONS D25); the night theme ships behind a
  manual toggle plus `prefers-color-scheme`, closing D21's recorded gap (D24); `app.css` is
  rewritten against V2 with shared `ContextBar`, `PageHeader`, `StatusChip`/`StatusLegend`,
  and `EmptyState` components. Routes and v2-styled skeletons were added for Map, Inventory,
  Configuration, and Provisioning **ahead of the epics that own their data** (E6, E1, E2, E4)
  — each renders only a header and a panel naming the epic that fills it. No mock devices,
  rows, or wizard steps: the point is that E1/E2/E4/E6 drop real data into a settled frame,
  not that the app looks finished.
- **Why it deviates:** the plan sequences DES.7 "after DES.4/DES.5 and the E2 UI". Only E0
  exists, so this batch does the half that does not need data — tokens, shell, theme,
  components — and stops at the boundary where invented content would start.
- **Deferred, noted here so it is not re-decided:** the **map engine** is not implemented.
  Direction is Google Maps satellite via the official JS API when online, an
  operator-supplied local image for air-gapped hosts (spec §15.1), ESRI later; E6 owns it.
  `Map.tsx` reserves the region and ships the real status legend around it. **Font
  vendoring** is also deferred — `tokens.css` names IBM Plex Sans/Mono and Source Serif 4
  with a fallback stack, and the woff2 files are not yet in `frontend/public/fonts/`, so the
  product currently renders in the fallbacks. **Image assets** land in
  `frontend/public/images/`: `forest-background.jpg` is present and backs **every** page via
  `.shell-content`, scrimmed by the new additive token `--eoe-color-backdrop-scrim` (0.93
  light / 0.94 dark) so it reads as texture and every documented contrast ratio still holds;
  the login hero keeps the far lighter `--eoe-color-overlay` and lets the photograph carry
  the screen. Both treatments paint a `background-color` first, so a missing file degrades to
  a flat token color rather than to unreadable text. The asset is committed web-sized
  (1920×1280, ~925 KB, EXIF stripped) rather than as the 6000×4000 / 24 MB camera original:
  it is fetched on every page, and git keeps binaries forever.
- **Also deferred:** the v1 bulk-edit modal (S4) and services onboarding wizard (S5) are
  flows nested inside Inventory and Provisioning, not top-level destinations in v2. They get
  built with E2.8 and E5.12 rather than being stubbed now.
- **Affects:** project_planning/DES-track-handoff.md ("DES.7 work items" 4-6, open question 2)
- **Addendum:** DES-7-01

## #8 (2026-07-30): DES.4 v2 tokens land; token namespaces gain an additive status/border/density extension

- **What changed:** `frontend/src/styles/tokens.css` and `tokens.alt.css` take the DES.4 v2
  ("field notebook") value set — same 30 property names, same five namespaces, values only.
  A third sheet, `frontend/src/styles/tokens.ext.css`, is accepted (DECISIONS D21, DES-4-01)
  and wired into `main.tsx`: it adds keys to the existing `--eoe-color-*`/`--eoe-space-*`/
  `--eoe-font-*` namespaces and introduces five new ones (`--eoe-border-width-*`,
  `--eoe-row-height-*`, `--eoe-control-height-*`, `--eoe-duration-*`, `--eoe-ease`), closing
  the six-value status vocabulary spec §9.3/§6.2 requires. Nothing in the original E0.4 key
  set is renamed, removed, or repointed. `frontend/tests/tokens.test.ts` now treats
  `tokens.ext.css` as a third application-owned sheet. Separately, `app.css`'s four uses of
  `var(--eoe-space-1)` as a border/outline width (rendering a 4px sidebar border, `.card`
  border, and both focus outlines) now use the new `--eoe-border-width-hairline: 1px`.
- **Why:** DES.4-handoff.md flagged this as a change to an E0-owned interface (rule R2) and
  asked for a verdict before applying it; the project owner accepted it in this session to
  finish the DES.4 deliverable. `tokens.alt.css` does not yet mirror the extended keys with
  dark-mode equivalents — recorded as a known, deliberately deferred gap in D21, not silently
  dropped.
- **Affects:** project_planning/phase-0-foundations.md section 2 (E0.4 token namespaces)
- **Addendum:** PHASE0-4-04

## #7 (2026-07-24): Top-level guide/ directory and the deployment verifier (Gate 14)

- **What changed:** The fixed repository layout gains a top-level `guide/` directory: the
  clearly demarcated, GitHub-prominent home for every client-facing artifact (operator
  quickstart, seed-script instructions with implications, deployment-verifier
  instructions). E0.12's deliverable also gains `backend/app/verify.py`
  (`uv run python -m app.verify`): a shippable owner-journey verifier that drives every E0
  subsystem through a temporary owner account over real HTTP and then deletes the
  account (audit rows deliberately survive with a nulled actor — immutability holds).
  No API surface changes; the locked route/table contracts are untouched.
- **Why:** Project-owner directive: client-facing parts must live in one demarcated,
  easy-to-find group, with USER instructions for the seed script, and epic-wide
  verification through a seeded owner account that is removed afterwards.
- **Affects:** project_planning/phase-0-foundations.md section 2 (repository layout)
- **Addendum:** PHASE0-2-01

## #6 (2026-07-24): E0 readiness flight added (Gate 13)

- **What changed:** A cross-cutting readiness suite (`backend/tests/test_e0_readiness.py`,
  Gate 13) verifies E0.1 through E0.11 as a whole: the exact public surface (routes and
  tables) as a locked contract, env-var parity between documentation and Settings, the
  seams later epics consume (E1's un-FK'd scope columns and MAC-wide entity ids, E3/E5's
  SecretStore name shapes, E8.5's session-minting seam for OIDC), a data-seeded migration
  round trip, and production posture (prod frontend image actually serves; API container
  runs non-root). Two defects found and fixed in the process: the compose frontend lacked
  `VITE_API_BASE_URL`, and the API image ran as root.
- **Why:** Project-owner directive: verify the platform is production-poised and that the
  infrastructure later epics build on is genuinely ready, not just unit-tested.
- **Affects:** project_planning/phase-0-foundations.md section 5 (definition of done)
- **Addendum:** PHASE0-5-01

## #5 (2026-07-24): E0.11 lands before E0.10

- **What changed:** The implementation order of the last two build tasks swaps: E0.11
  (platform secrets envelope encryption) precedes E0.10 (optional TOTP). Task content is
  unchanged; gate tags stay bound to task ids (gate-11 completes before gate-10 in
  history).
- **Why:** A TOTP secret is a secret, and the cross-phase convention (rule R2, handbook
  section 3) requires secrets to move only through `SecretStore`. Building TOTP first would
  either violate that convention or store the secret plaintext and re-encrypt later.
- **Affects:** project_planning/phase-0-foundations.md section 4 (E0.10, E0.11)
- **Addendum:** PHASE0-4-03

## #4 (2026-07-23): E0.8 spec citation corrected

- **What changed:** The phase document's E0.8 task cites "spec 12.1" as the source of the
  audit-log requirement; section 12.1 covers tenancy. The requirement actually derives from
  spec section 14.1 (immutable audit log of every mutation) and section 13 (`GET /audit`).
- **Why:** Later sessions follow section references literally; an incorrect citation sends
  them to the wrong requirement.
- **Affects:** project_planning/phase-0-foundations.md section 4 (E0.8)
- **Addendum:** PHASE0-4-02

## #3 (2026-07-23): Session inputs are three documents, not two

- **What changed:** Project plan section 5 says each implementation session receives "exactly
  two inputs: spec v1.1 and the phase document." The implementation handbook (section 2), the
  later and operational document, specifies three inputs plus one conditional: the spec, the
  phase document, the current `docs/INTERFACES.md`, and `docs/DECISIONS.md` once it has
  content. The handbook's list governs.
- **Why:** The two documents disagree; sessions must know which list to follow.
- **Affects:** project_planning/echoes-of-earth-project-plan.md section 5
- **Addendum:** PLAN-5-01

## #2 (2026-07-23): E0.12 Seed script missing from the project plan

- **What changed:** Project plan section 3 lists eleven E0 tasks (E0.1 through E0.11). The
  phase document defines twelve; E0.12 (dev seed creating the initial owner account,
  credentials printed once, fresh environment to logged-in owner in one command) exists only
  in the phase document. The Jira board needs the extra story.
- **Why:** Task inventory drift between the plan and the phase document would surface as a
  missing story on the board.
- **Affects:** project_planning/echoes-of-earth-project-plan.md section 3 (E0)
- **Addendum:** PLAN-3-01

## #1 (2026-07-23): Task E0.0 (governance scaffolding) added before E0.1

- **What changed:** A task E0.0 precedes E0.1: binding machine-readable project rules
  (`.claude/rules/project-rules.json`), the `CLAUDE.md` loader, the three project logs
  (`docs/project-updates.md`, `docs/project-changes.md`, `docs/DECISIONS.md`), the
  `docs/INTERFACES.md` skeleton, the git baseline of `project_planning/`, the commit message
  template, and the gate runner. E0.0 ends in Gate 0, subject to rule R0 like every task.
- **Why:** The project is built as fresh scope-limited sessions (handbook section 2); the
  governing rules and the running record must live in the repository rather than in any one
  conversation, and they must exist before the first build task.
- **Affects:** project_planning/phase-0-foundations.md section 4
- **Addendum:** PHASE0-4-01
