# Typefaces

Served from `/fonts/…` at runtime and declared in `src/styles/fonts.css`. Vite copies
`public/` verbatim; these are not bundled.

**Vendored, never fetched.** Spec §15.1 puts this platform on self-hosted and air-gapped
installations. A webfont from a CDN there is not a slow font, it is a missing font — so
there is no Google Fonts URL and no `@import` anywhere in the styles, and
`frontend/tests/fonts.test.ts` fails the gate if one appears.

| File                                    | Family            | Weight | Role                                     |
| --------------------------------------- | ----------------- | ------ | ---------------------------------------- |
| `ibm-plex-sans-latin-400-normal.woff2`  | IBM Plex Sans     | 400    | UI, body, tables                         |
| `ibm-plex-sans-latin-500-normal.woff2`  | IBM Plex Sans     | 500    | medium emphasis, chips                   |
| `ibm-plex-sans-latin-600-normal.woff2`  | IBM Plex Sans     | 600    | bold                                     |
| `ibm-plex-mono-latin-400-normal.woff2`  | IBM Plex Mono     | 400    | identifiers: MACs, UUIDs, topics, hashes |
| `ibm-plex-mono-latin-600-normal.woff2`  | IBM Plex Mono     | 600    | uppercase eyebrow labels                 |
| `source-serif-4-latin-600-normal.woff2` | Source Serif 4    | 600    | display only: page titles, hero metric   |
| `eoe-status-glyphs.woff2`               | EOE Status Glyphs | 400    | the six status shapes, nothing else      |

Latin subsets only, ~160 KB for the whole set. Only weights the CSS actually uses are
vendored: a face appearing at a weight that is not in this table is being synthesised, which
is a styling bug rather than a missing file.

## eoe-status-glyphs.woff2 — why a seventh file exists

Status is three channels: color + shape + label. The shape is what survives greyscale, color
blindness, and a tablet in direct sun. The six shapes are Geometric Shapes and Dingbats
codepoints — `●` U+25CF, `◐` U+25D0, `▲` U+25B2, `■` U+25A0, `✕` U+2715, `◆` U+25C6 — and
**none of them exists in IBM Plex Sans, IBM Plex Mono, or Source Serif 4**, in the full
families, not merely in these subsets. Left to system fallback they render inconsistently
across platforms and become tofu on a minimal air-gapped host, which deletes the channel
without any visible error.

So the shapes are vendored too: Noto Sans Symbols 2 (OFL 1.1), subsetted to exactly those six
codepoints. 568 bytes. `--eoe-font-family-glyph` names it and `.status-glyph::before` is its
only consumer.

### Re-cutting it (adding or changing a status glyph)

`fonts.test.ts` check 4 compares the glyph tokens in `tokens.ext.css` against this file's
declared `unicode-range` and fails the gate when they diverge. After changing a glyph token,
re-cut the subset and widen the range in `fonts.css`:

```
pip install fonttools brotli
curl -LO https://github.com/google/fonts/raw/main/ofl/notosanssymbols2/NotoSansSymbols2-Regular.ttf
pyftsubset NotoSansSymbols2-Regular.ttf \
  --unicodes=U+25CF,U+25D0,U+25B2,U+25A0,U+2715,U+25C6 \
  --flavor=woff2 --no-hinting --desubroutinize --name-IDs='' \
  --output-file=eoe-status-glyphs.woff2
```

Pick shapes that stay distinct as a 10px mark: fill and silhouette, not orientation. Two
glyphs that differ only by rotation are one channel, not two.

## Licenses

All three families are SIL Open Font License 1.1; the license text ships beside the fonts as
the OFL requires.

| Source                                             | License file                      |
| -------------------------------------------------- | --------------------------------- |
| IBM Plex Sans / Mono (subsets built by Fontsource) | `LICENSE-ibm-plex.txt`            |
| Source Serif 4 (Adobe; subset built by Fontsource) | `LICENSE-source-serif-4.txt`      |
| Noto Sans Symbols 2 (Noto Project)                 | `LICENSE-noto-sans-symbols-2.txt` |
