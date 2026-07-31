/**
 * Vendored-typeface checks (DES.4 "three rules" item 3; spec §15.1).
 *
 * Two things can silently break the typography on an air-gapped host, and
 * neither shows up in a browser with a network and a full system font set:
 * a face fetched from a CDN, and a face named in a token that no @font-face
 * ever supplies. Both fail the gate here.
 *
 * The third check is the one that matters most: the status vocabulary's
 * shape channel. Its glyphs exist in none of the text families, so they ship
 * as their own subset — and a seventh status added later without re-cutting
 * that subset would render as tofu, deleting the channel that carries status
 * for a greyscale, color-blind, or sun-washed reader.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "src");
const PUBLIC_FONTS = join(HERE, "..", "public", "fonts");
const FONT_SHEET = join(SRC, "styles", "fonts.css");
const APP_SHEET = join(SRC, "styles", "app.css");
const TOKEN_SHEET = join(SRC, "styles", "tokens.css");
const EXT_SHEET = join(SRC, "styles", "tokens.ext.css");

const read = (f: string) => readFileSync(f, "utf-8");
const fontCss = read(FONT_SHEET);

/** Every @font-face block in fonts.css, as raw text. */
const faces = [...fontCss.matchAll(/@font-face\s*\{([^}]*)\}/g)].map((m) => m[1]);

const familyOf = (face: string) => face.match(/font-family:\s*"([^"]+)"/)?.[1];

const GLYPH_FAMILY = "EOE Status Glyphs";

describe("vendored fonts", () => {
  it("declares at least the five text faces plus the glyph face", () => {
    expect(faces.length).toBeGreaterThanOrEqual(6);
  });

  // Check 1: every src resolves to a file that is actually committed.
  it("points every @font-face at a committed file under public/fonts", () => {
    const urls = [...fontCss.matchAll(/url\("([^"]+)"\)/g)].map((m) => m[1]);
    expect(urls.length, "no @font-face src urls found").toBe(faces.length);
    for (const url of urls) {
      expect(url, `${url} is not served from /fonts/`).toMatch(/^\/fonts\//);
      const path = join(PUBLIC_FONTS, url.replace("/fonts/", ""));
      expect(existsSync(path), `${url} has no file at public/fonts`).toBe(true);
      expect(readFileSync(path).length, `${url} is empty`).toBeGreaterThan(0);
    }
  });

  // Check 2: vendored means vendored. No CDN, on any sheet. Comments are
  // stripped first — these sheets explain the rule in prose, and matching the
  // explanation instead of the code is a false positive.
  it("fetches no font from the network", () => {
    for (const sheet of [FONT_SHEET, APP_SHEET, TOKEN_SHEET, EXT_SHEET]) {
      const css = read(sheet).replace(/\/\*[\s\S]*?\*\//g, "");
      expect(css, `remote url in ${sheet}`).not.toMatch(/url\(\s*["']?https?:/);
      expect(css, `@import in ${sheet}`).not.toMatch(/@import/);
    }
  });

  // Check 3: a token may not name a first-choice family nothing supplies.
  // Later families in each stack are deliberate system fallbacks.
  it("supplies an @font-face for the first family of every font-family token", () => {
    const declared = new Set(faces.map(familyOf));
    for (const sheet of [TOKEN_SHEET, EXT_SHEET]) {
      for (const match of read(sheet).matchAll(/--eoe-font-family[a-z-]*:\s*"([^"]+)"/g)) {
        expect(declared.has(match[1]), `no @font-face for "${match[1]}" (${sheet})`).toBe(true);
      }
    }
  });

  // Check 4: the glyph subset covers exactly the status glyphs in use.
  it("covers every status glyph in the glyph subset's unicode-range", () => {
    const glyphFace = faces.find((f) => familyOf(f) === GLYPH_FAMILY);
    expect(glyphFace, `no @font-face for "${GLYPH_FAMILY}"`).toBeDefined();
    const covered = new Set(
      [...glyphFace!.matchAll(/U\+([0-9A-Fa-f]+)/g)].map((m) => parseInt(m[1], 16)),
    );
    const used = [...read(EXT_SHEET).matchAll(/--eoe-color-status-[a-z]+-glyph:\s*"(.+?)"/g)].map(
      (m) => m[1].codePointAt(0)!,
    );
    expect(used.length, "no status glyph tokens found").toBeGreaterThanOrEqual(6);
    for (const cp of used) {
      const hex = `U+${cp.toString(16).toUpperCase().padStart(4, "0")}`;
      expect(
        covered.has(cp),
        `status glyph ${hex} (${String.fromCodePoint(cp)}) is outside the vendored subset — ` +
          `re-cut eoe-status-glyphs.woff2, see public/fonts/README.md`,
      ).toBe(true);
    }
  });

  // Check 5: the wiring itself. The glyph family has one consumer, and if a
  // restyle drops it the glyphs quietly fall back to system fonts again.
  it("renders the status glyph in the glyph family", () => {
    const rule = read(APP_SHEET).match(/\.status-glyph::before\s*\{([^}]*)\}/)?.[1];
    expect(rule, ".status-glyph::before rule not found in app.css").toBeDefined();
    expect(rule).toMatch(/font-family:\s*var\(--eoe-font-family-glyph\)/);
  });
});
