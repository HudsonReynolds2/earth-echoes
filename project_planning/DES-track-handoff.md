# Echoes of Earth — design handoff

**Track:** DES · **Date:** 2026-07-30 · **Stage:** DES.1–DES.6 delivered, DES.7 ready to start

## Where things stand

| Task | State |
|---|---|
| DES.1 screen and flow inventory | **Done** — 18 surfaces mapped to epics and priority, in `Design System.dc.html` |
| DES.2 / DES.3 direction candidates and decision | **Done** — v1 "field instrument" reviewed and rejected, v2 "field notebook" is the committed direction |
| DES.4 token sheet | **Done, including DES-4-01.** `tokens.css`/`tokens.alt.css` v2 landed; the additive `tokens.ext.css` extension is accepted (`docs/DECISIONS.md` D21) and wired into `main.tsx`. Known gap: `tokens.alt.css` doesn't yet carry dark-mode equivalents of the extended keys. |
| DES.5 component library | **Done for v1 values, v2 for the parts used in V2·S1–S3** — buttons, badges, markers, tree, grid, wizard steps, states |
| DES.6 UX pass | **Owner dashboard, map + drawer, night theme at v2.** Config editor, bulk edit, services wizard, provisioning board exist at v1 and need the mechanical v2 restyle |
| DES.7 apply to frontend | **Not started** — unblocked; this doc is the input |
| DES.8 usability review of the built UI | Blocked on E4–E6 |

## Files to take

```
design/tokens.css       → frontend/src/styles/tokens.css       (landed, gate-safe)
design/tokens.alt.css   → frontend/src/styles/tokens.alt.css   (landed; now a real night theme)
design/tokens.ext.css   → frontend/src/styles/tokens.ext.css   (landed; DES-4-01 accepted, see D21)
design/DES.4-handoff.md → project_planning/DES.4-handoff.md (value rationale; DES-4-01 itself now lives in docs/DECISIONS.md D21)
Design System.dc.html     DES.1 inventory, DES.5 component library  (v1 values)
Screens.dc.html           S1–S7 full screen set                    (v1 values)
Screens v2.dc.html        V2·S1–S3: the committed look, incl. night theme
```

Where v1 and v2 disagree, **v2 wins**. v1 is kept only because it holds screens v2 has not
redrawn yet, and their layout is still correct.

## The three rules the token sheet encodes

1. **Interactive is ink, not color.** Primary buttons are near-black `--eoe-color-action`.
   Teal `--eoe-color-accent` is links, selection, and focus only. No button ever takes a
   status color, so a colored button can never be mistaken for a colored status.
2. **Status is three channels, never one.** Color + shape glyph + text label, on every badge,
   marker, dot, and chip. The colors are Okabe–Ito derived and ordered by lightness so the
   set survives greyscale. **If you implement only the hex values and drop the glyphs, the
   palette is not accessible** — the glyph is the accessibility, the color is the shortcut.
3. **Fonts are vendored, never fetched.** Spec §15.1 puts this on air-gapped hosts. Put the
   woff2 files in `frontend/public/fonts/` and point `@font-face` at them: Source Serif 4
   (display only — page titles and the one hero number per screen), IBM Plex Sans (UI, body,
   tables), IBM Plex Mono (identifiers: MACs, UUIDs, topics, checksums, timestamps).

## DES.7 work items, in order

1. ~~**Swap the two token sheets.**~~ **Done.** Values only, no name changes; `tokens.test.ts`
   checks 1–6 pass unchanged and `e2e/theme-swap.spec.ts` is untouched.
2. ~~**Decide DES-4-01**~~ **Done — accepted, additive.** See `docs/DECISIONS.md` D21. The
   status system is built inside the new `tokens.ext.css` sheet; `danger`/`success`/`warning`
   keep their names and alias to the matching statuses (gate-checked, `tokens.test.ts` check
   7). Known gap: `tokens.alt.css` doesn't carry dark-mode equivalents of the extended keys
   yet — a real design task (per-pair contrast verification), not done in this batch.
3. ~~**Fix the border width bug, independent of everything above.**~~ **Done.** `app.css`
   wrote `border`/`outline: var(--eoe-space-1) solid …` in four places for want of a width
   token, rendering the sidebar, `.card`, and both focus outlines at **4px**. Now uses
   `--eoe-border-width-hairline: 1px`.
4. **Restructure the shell.** `Shell.tsx` is a left sidebar; V2·S1 is a dark top bar with
   horizontal nav plus a breadcrumb row. The map needs the full width, and the hierarchy
   breadcrumb needs a permanent home the sidebar cannot give it. This is the one structural
   change in DES.7 — everything else is CSS.
5. **Add the theme toggle.** Night theme ships behind `prefers-color-scheme` *plus* a manual
   override. The manual override is not optional: field staff read this outdoors in daylight
   where the OS setting is wrong.
6. **Restyle the four remaining v1 screens** to v2 (config editor, bulk edit, services
   wizard, provisioning board). Mechanical: palette, type sizes, status badges with glyphs,
   36px rows. Ask me and I redraw them first if you would rather not eyeball it.

## Open questions I still need answered

1. ~~**DES-4-01 verdict.**~~ Resolved — accepted, see D21.
2. **Does a Field Tech see telemetry at all?** I read spec §12.3 "no telemetry depth" as: sees
   status, no Grafana embeds, Telemetry tab present but disabled.
3. **Is the config editor ever used on tablet?** It is the one screen I would leave
   desktop-only.

> **Addendum DES-7-01 (2026-07-30, ref project-changes #9):** Items 4 and 5 are done — the shell is V2·S1's dark top bar (DECISIONS D25), and the night theme ships behind a manual toggle plus `prefers-color-scheme`, with `tokens.ext.alt.css` closing D21's dark-palette gap (D24). Item 6 is **not** done as written: rather than restyle the four v1 screens with invented content, Map, Inventory, Configuration, and Provisioning ship as routed, v2-styled skeletons naming the epic that brings their data. Open question 2 (does a Field Tech see telemetry?) is superseded for navigation purposes by D25 — the primary nav lists every destination for every role and pages gate their own contents; the disabled-tab treatment is deferred to DES.8. Open question 3 (config editor on tablet) is still open. Also deferred: the map engine (E6 owns it; direction recorded in project-changes #9) and font vendoring — the woff2 files named in "The three rules" are still absent, so the product renders in the fallback stack.

## Known caveat

None of these mockups has been visually QA'd in-tool: the preview screenshot bridge in my
environment times out on even a trivial static HTML file, so my review and the verifier's
were source-level only. Structure, tokens, contrast ratios, and markup are checked. Pixel
layout, overflow, and clipping inside the 1440×900 frames are not. Please eyeball
`Screens v2.dc.html` before building against it.
