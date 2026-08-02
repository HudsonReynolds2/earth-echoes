# DES.4 handoff — token sheet and the one contract question

**Date:** 2026-07-30 · **Track:** DES · **Reads:** spec v1.1 §3.2, plan v1.0 DES.1–DES.8,
`docs/INTERFACES.md` "Design tokens" (E0-owned), `frontend/src/styles/tokens.css`,
`tokens.alt.css`, `app.css`

**Status: resolved.** DES-4-01 (below) is decided — accepted, additive-only — and applied.
The binding record now lives in `docs/DECISIONS.md` D21, `docs/INTERFACES.md` "Design
tokens", and `docs/project-changes.md` #8; this document is kept for the design rationale
behind the value choices, which those records don't restate. Where this document and D21
disagree, D21 governs.

## What is in this folder

| File | Drop-in target | Contract impact |
|---|---|---|
| `tokens.css` | `frontend/src/styles/tokens.css` | **None.** Same 30 property names, same order, same five namespaces. Values only. |
| `tokens.alt.css` | `frontend/src/styles/tokens.alt.css` | **None.** Mirrors the same key set exactly. |
| `tokens.ext.css` | *nothing yet* | **Additive.** Needs DECISIONS + INTERFACES amendment first. Draft entry below. |

`tokens.css` and `tokens.alt.css` are gate-safe and mergeable today. They are also
independently useful: swapping them alone takes the frontend from neutral gray to the
committed direction, and `e2e/theme-swap.spec.ts` should pass unchanged.

## Value decisions worth knowing before you merge

1. **Blue is the only interactive color; green is reserved for status.** `--eoe-color-accent`
   is blue and `--eoe-color-success` is green, and no button anywhere is green. This is load
   bearing — a green button in a product whose map uses green for "healthy" teaches the
   operator the wrong thing. Please don't "improve" it later without reading this line.
2. **Correction (caught while reconciling this doc against the committed file):** an earlier
   draft of this section claimed `--eoe-font-size-md` drops 15px → 13px and `sm` 13px → 12px.
   That is not what shipped — checked against `frontend/src/styles/tokens.css`, `sm` (13px)
   and `md` (15px) are **unchanged** from the E0.4 neutral baseline; `lg` and `xl` grew
   (18px → 19px, 24px → 26px) instead. The single biggest visual change in the set is
   density from the bigger space rungs (`--eoe-space-6`/`-8`) used for panel and section
   spacing, not a smaller type scale. If a page reads too tight or too loose against the
   spec §5/§9 28px device-grid rows, the fix is `--eoe-space-*` usage in that component's
   CSS, not the font sizes.
3. **No webfont.** `--eoe-font-family` is a Helvetica/Arial stack, not a licensed pairing.
   Spec §15.1 puts this platform on air-gapped self-hosted hosts with no font CDN to reach,
   so a `@font-face` download is a broken-typography risk, not a nicety. If you want IBM Plex
   Sans / Plex Mono (my first choice on merit), it ships as vendored local files and changes
   only these two values.
4. **`--eoe-color-surface-raised` is now *darker* than `surface`, not lighter.** On a light
   theme, "raised" reads as a recessed tint in every real use in `app.css` (nav hover). The
   name is E0-owned so I did not touch it; flagging that the name and the behavior disagree.
5. **`--eoe-color-focus` now equals `--eoe-color-accent`.** One focus color, legible on both
   `bg` and `surface`. The old navy against near-black text was hard to spot.

## The one contract question: six statuses, three color names

Spec §9.3 (map/status) and §6.2 (revision states) between them define **six** device states
that must render identically as a map marker, a table cell, and a revision chip:

`streaming/healthy` · `sleeping` · `degraded` · `offline` · `alerting` · `drifted`

Each needs a solid (marker fill, dot, text) and a tint (badge background). The locked key
set offers `danger` / `success` / `warning` — three names, no tints. Consequences if we
ship as-is:

- `sleeping`, `offline`, and `drifted` have no token at all. §6.5 makes `sleeping` a
  *healthy* state distinct from `offline`, and §6.2 makes `drifted` distinct from `failed`;
  collapsing them loses information the operator needs.
- Every badge background becomes a literal hex, which fails `tests/tokens.test.ts`. There
  is no gate-compliant way to build the status system inside the current key set.

`frontend/src/styles/tokens.ext.css` carries the additive set. **Nothing is renamed, removed,
or repointed** — 12 status names, 3 accent-state names, 3 neutrals, 2 border widths, 3 space
rungs, 4 type constants, 5 density constants, 3 motion. `danger`/`success`/`warning` keep
their current names and are aliased to the matching status colors; the alias is documented
convention rather than a mechanical `var()` reference (that would couple `tokens.css` to the
extension), so `frontend/tests/tokens.test.ts` check 7 gate-enforces the two staying equal.

**Resolved.** DES-4-01 was raised as a change to an E0-owned interface (rule R2) and is now
accepted, additive-only — see `docs/DECISIONS.md` D21 for the full decision text, alternatives
considered, and the one open follow-up (`tokens.alt.css` does not yet carry dark-mode
equivalents of these keys). The border-width fix below shipped in the same change.

**Separable sub-item, fixed regardless of the above: border width.** `app.css` wrote
`border`/`outline: var(--eoe-space-1) solid …` in four places (not three — the sidebar
border, the `.card` border, *and both* focus-visible outlines) because no width token
existed, rendering a **4px** border/outline everywhere it appeared. All four now use
`--eoe-border-width-hairline: 1px`.

## What I need next

Items below are what the design session originally needed from a fresh session with fuller
repo access; kept for anyone picking up DES.7 next.

1. ~~**Verdict on DES-4-01.**~~ Resolved — see above.
2. **`Shell.tsx`, `App.tsx`, and `pages/*.tsx`** — already in the repo
   (`frontend/src/components/Shell.tsx`, `frontend/src/App.tsx`) for a session with normal
   repo access; still needed reading before DES.7's shell restructuring (dark top bar plus
   breadcrumb row, per `project_planning/DES-track-handoff.md`) becomes a concrete diff. Not touched in this batch
   — DES.7 is unstarted and out of scope here, this batch was DES.4 only.
   **Update (2026-08-01):** DES.7 has since been applied (Gates 16–18, DECISIONS D25); this
   item is closed.
3. ~~**`frontend/tests/tokens.test.ts`**~~ Already in the repo and now widened for the D21
   extension (checks 3, 5, 7 above).
4. **Eyeball S1–S7** in `Screens.dc.html`. Still open — that file isn't in this repo, so it
   wasn't available to check in this batch either. The preview bridge issue noted originally
   means the mockups have not been visually QA'd; source is sound, pixels are unverified.
   **Update (2026-08-01):** `Screens.dc.html` and `Screens v2.dc.html` **are now in the repo**
   — committed to `project_planning/` at Gate 16. The eyeball item itself remains open and is
   inherited by DES.8 (`DES-track-handoff.md`, addendum DES-7-02).
