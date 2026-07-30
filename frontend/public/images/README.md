# Image assets

Served from `/images/…` at runtime. Vite copies `public/` verbatim; these are not bundled.

| File                        | Used by                                                | Status                                                                                                                                             |
| --------------------------- | ------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `forest-background.jpg`     | Login backdrop (`.login-page` in `src/styles/app.css`) | **Drop it here.** Absent today; the page falls back to a flat `--eoe-color-action` behind the overlay, so a missing file is never a broken layout. |
| `satellite-placeholder.jpg` | Offline map imagery                                    | Reserved for E6. Nothing loads it yet.                                                                                                             |

Landscape, ≥1920px wide, and dark or mid-tone: the login card sits on top of a
`--eoe-color-overlay` scrim, and a bright image will fight it.
