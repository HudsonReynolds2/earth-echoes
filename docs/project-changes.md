# Project Changes

Numbered, reverse-chronological, append-only log of changes to scope, sequencing, task
definitions, or acceptance criteria relative to the planning documents (rule R1,
`.claude/rules/project-rules.json`). Every entry names an addendum that exists in the
referenced planning document; an entry with no addendum is incomplete.

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
