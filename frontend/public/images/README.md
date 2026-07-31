# Image assets

Served from `/images/…` at runtime. Vite copies `public/` verbatim; these are not bundled.

| File                        | Used by                                                          | Status                                 |
| --------------------------- | ---------------------------------------------------------------- | -------------------------------------- |
| `forest-background.jpg`     | Every page (`.shell-content`) and the login hero (`.login-page`) | **Present.** 1920×1280, ~925 KB.       |
| `satellite-placeholder.jpg` | Offline map imagery                                              | Reserved for E6. Nothing loads it yet. |

## forest-background.jpg

Backs the whole application below the top bar. Two treatments share one file:

- **Every page** scrims it with `--eoe-color-backdrop-scrim` (0.93 light / 0.94 dark), so it
  reads as texture and content keeps the contrast ratios the token sheet documents.
- **The login hero** scrims it with the far lighter `--eoe-color-overlay`, letting the
  photograph carry the screen — the only content there is an opaque card.

Both treatments set a `background-color` first, so a missing file degrades to a flat token
color rather than to unreadable text.

### Replacing it

Landscape, dark or mid-tone (a bright image fights the login scrim), and **resized for the
web before committing** — camera originals are tens of megabytes and this file is fetched on
every page:

```
convert original.jpg -resize 1920x -strip -quality 80 -interlace Plane forest-background.jpg
```

`-strip` also drops EXIF, which on a camera original carries the capture location.
