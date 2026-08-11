# Frontend guide

For anyone working on the look, layout, or behavior of the Echoes of Earth web app. Covers
running it, where each kind of change belongs, and the rules the test suite enforces.

Everything here lives under `frontend/`. Nothing in this guide requires the backend, Docker,
or Python.

> **Normative source: `docs/INTERFACES.md`, "Frontend composition and shared components".**
> That section is the contract for the shell, the shared components, the two page shapes,
> and the composition rules — read it before touching `frontend/src`; it is what stops a new
> session rebuilding what already exists. This guide is the tour, not the contract: where
> the two disagree, INTERFACES.md wins.

## Run it

Node 20+ and git are the only prerequisites.

```bash
git clone <repo-url> earth-echoes
cd earth-echoes/frontend
npm ci
VITE_API_BASE_URL=http://localhost:18000 npm run dev   # → http://localhost:15173
```

`VITE_API_BASE_URL` is required — the app throws without it, by design (no hardcoded API
URL, no dev proxy). For repeat runs, put `VITE_API_BASE_URL=http://localhost:18000` in
`frontend/.env.local` (gitignored) and just run `npm run dev`.

**With no backend running, every route still renders.** Routes are not auth-gated, so
`/`, `/map`, `/inventory`, `/configuration`, `/provisioning`, `/system`, `/users`, and
`/login` are all reachable and every visual surface — nav, theme toggle, page headers, cards,
forms, tables, empty states — is designable. Only live data areas show their loading and
error states, which are worth designing anyway.

Want real data? Copy `deploy/.env.example` to `deploy/.env`, fill every value, then
`docker compose -f deploy/docker-compose.yml up -d --build`. See the root `README.md`.

## Before you push

```bash
npm run lint    # eslint + prettier --check   (npm run format fixes formatting)
npm test        # vitest — includes the token-discipline and font suites
```

Both must pass. The full repo gate (`make gate`, or `./gate.ps1` on Windows) runs these plus
the backend suite; it must be green before anything is committed. Work on a branch, never
directly on `main`, one pull request per batch.

Browser tests are separate and need a one-time browser install:

```bash
npx playwright install --with-deps
npm run test:e2e
```

## The map

### Design system

| Path | What it holds |
| --- | --- |
| `src/styles/tokens.css` | The 30 locked tokens — color, space, font, radius, shadow (light) |
| `src/styles/tokens.ext.css` | Additive tokens — status palette + glyphs, ink/action colors, density, motion, tracking |
| `src/styles/tokens.alt.css` | Dark-theme values for `tokens.css` |
| `src/styles/tokens.ext.alt.css` | Dark-theme values for `tokens.ext.css` |
| `src/styles/fonts.css` | `@font-face` for the vendored woff2 files |
| `src/styles/app.css` | All layout and component CSS, sectioned: shell → context band → page body → status → map → forms → login → tables |
| `public/fonts/`, `public/images/` | The woff2 files and `forest-background.jpg`; each folder has its own README |

### Markup and behavior

`src/components/` — `Shell.tsx` (top bar and primary nav), `ContextBar.tsx`, `PageHeader.tsx`,
`StatusChip.tsx`, `EmptyState.tsx`, `ThemeToggle.tsx`, `Can.tsx`
`src/pages/` — `Login.tsx`, `Overview.tsx`, `Map.tsx`, `UsersAdmin.tsx`, `SystemStatus.tsx`,
plus placeholder pages for Inventory, Configuration, Provisioning
`index.html` — document title and meta

### Context worth reading first

- `project_planning/DES-track-handoff.md` — state of the design track and the three binding rules
- `project_planning/DES.4-handoff.md` — why the current token values are what they are
- `project_planning/Screens v2.dc.html` — the committed look; opens straight in a browser
- `project_planning/Screens.dc.html` — full S1–S7 screen set (older values, layout still valid)

## Where to change what

| Change | File |
| --- | --- |
| Any color, light theme | `tokens.css` (core) or `tokens.ext.css` (status, action, tints) |
| Any color, dark theme | `tokens.alt.css` / `tokens.ext.alt.css` — same key, dark value |
| Type scale, weights, tracking, line height | `tokens.css` (`--eoe-font-*`); display serif and `2xs`/`2xl` in `tokens.ext.css` |
| Swap a typeface | Add the woff2 to `public/fonts/`, edit `fonts.css`, repoint `--eoe-font-family*` |
| Spacing scale, radius, shadow | `tokens.css`; extra rungs (`space-5/10/12`, pill, round) in `tokens.ext.css` |
| Row, control, and chrome-band heights | `tokens.ext.css` (`--eoe-row-height-*`, `--eoe-control-height-*`, `--eoe-height-*`) |
| Transition speed and easing | `tokens.ext.css` (`--eoe-duration-*`, `--eoe-ease`) |
| Layout, grid, alignment, per-component styling | `app.css` — but only through `var(--eoe-*)` |
| Nav items, page structure, markup, interaction | The `.tsx` files in `components/` and `pages/` |
| Background photograph | Replace `public/images/forest-background.jpg` (its README has the resize command); scrim strength is `--eoe-color-backdrop-scrim` |

**Adding a new token.** New custom properties go in `tokens.ext.css` only — the test suite
requires every `var(--eoe-*)` reference to resolve against `tokens.css` or `tokens.ext.css`.
If the new token is a color, add its dark value to `tokens.ext.alt.css` in the same commit.

## Rules the tests enforce

These fail `npm test`, so they are worth knowing before you start rather than after.

1. **No raw values outside the four token sheets.** No hex, `rgb()`/`hsl()`, or named colors
   anywhere in `app.css` or the `.tsx` files, and no `px`/`rem` literals on `margin`,
   `padding`, `gap`, `border-radius`, `box-shadow`, or `font-size`. Everything routes through
   `var(--eoe-*)`. This is what keeps a restyle a value swap instead of a refactor.
2. **The dark sheets mirror the light ones.** `tokens.alt.css` must define exactly the same
   key set as `tokens.css`; `tokens.ext.alt.css` must be a strict subset of `tokens.ext.css`
   (it overrides values, it never introduces keys). Both must stay scoped to
   `:root[data-theme="dark"]` — a bare `:root` in either would make dark mode win everywhere.
3. **Themes are selected by attribute, not by import.** `src/lib/theme.ts` sets
   `document.documentElement.dataset.theme`; only `main.tsx` may import the night sheets. No
   component imports a theme sheet.
4. **`--eoe-color-danger` / `success` / `warning` must stay byte-identical** to
   `--eoe-color-status-alerting` / `healthy` / `degraded` in the same sheet. They are
   documented aliases and the test enforces it in both themes.
5. **Fonts are vendored, never fetched.** Any CDN URL, `@import`, or `src` naming a file that
   is not committed fails the gate. The platform is specified for air-gapped hosts, where a
   fetched webfont is a missing font, not a slow one.
6. **Status is three channels: color, glyph, and text label — always all three.** The Okabe–Ito
   colors are the shortcut; the shape glyph is the accessibility. A seventh status also needs
   the glyph subset font re-cut, or it renders as tofu.

Two conventions the tests do not check but the codebase holds to: interactive elements are
ink (`--eoe-color-action`, near-black), never a status color — a green button in a product
whose map uses green for "healthy" teaches the wrong thing; and the serif is display only,
for page titles and the single hero number per screen, never body text or tables.

## Starting E1

Pointers for the first data-bearing epic, so the settled frame gets extended rather than
rebuilt:

- **Read `docs/INTERFACES.md` "Frontend composition and shared components" first** — the
  shell, the page shapes, and the shared components already exist; E1 drops data into them.
- **Need a new value? Add a token to `tokens.ext.css`** (dark value in `tokens.ext.alt.css`
  in the same commit). Never rename an existing token — the names are an E0-owned,
  gate-enforced contract (rules 1–4 above).
- **Device status is a closed six-state vocabulary**, always color + shape + label — use
  `<StatusChip>`/`<StatusLegend>`, never a hand-rolled badge. A seventh state means
  re-cutting the glyph subset first (`public/fonts/README.md` has the recipe), or it ships
  as tofu (rule 6).
- **Mockups: take v1 for layout, v2 for values.** `Screens v2.dc.html` covers only three
  screens (V2·S1–S3); `Screens.dc.html` holds the full S1–S7 set at older values. None of
  the mockups have been visually QA'd (`DES-track-handoff.md`, "Known caveat") — eyeball
  before building against them.
- **The skeleton pages hold no mock data on purpose.** Replace the `EmptyState` "arrives
  with E1" panel with the real surface; don't restyle around it.
- **E1 starters (shipped at gates 27-28):** `ContextBar` carries the live breadcrumb
  (crumbs are real links since D41); the table pattern is now the shared `.data-table`
  (D42 — `.admin-table` is gone; do not start a second vocabulary); TanStack **Table**
  is installed (D39, headless, server-driven). The inventory surfaces are live —
  `src/pages/inventory/` and `src/lib/inventory.ts` are the patterns E2+ extend, and
  the no-fabricated-status guard (D40) stays until E3 lands real state.

## House style

TypeScript everywhere, no bare `any` without a comment explaining it. Prettier with
`printWidth: 100` — run `npm run format` rather than hand-wrapping. Match the existing
linters; they were settled in phase 0 and are not up for relitigation.
