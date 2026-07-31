/**
 * Gate 4 token-discipline checks (task E0.4; spec 3.2).
 *
 * These scans are what keep DES.7 a value swap instead of a refactor: a
 * single leaked color or spacing literal outside the token sheet would turn
 * applying the design system into a code change.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "src");
const TOKEN_SHEET = join(SRC, "styles", "tokens.css");
const ALT_SHEET = join(SRC, "styles", "tokens.alt.css");
// Additive extension accepted under DECISIONS D21 (DES-4-01): a third,
// application-owned sheet, imported by main.tsx alongside tokens.css.
const EXT_SHEET = join(SRC, "styles", "tokens.ext.css");
// Night-theme values for the extension (DECISIONS D24), scoped to
// :root[data-theme="dark"]. Closes D21's recorded gap.
const EXT_ALT_SHEET = join(SRC, "styles", "tokens.ext.alt.css");
// Both alt sheets are shipped, but only main.tsx may pull them in: the theme
// is chosen by lib/theme.ts flipping the data-theme attribute, never by a
// component importing a sheet.
const THEME_ENTRYPOINT = join(SRC, "main.tsx");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

const allFiles = walk(SRC);
const styleAndCodeFiles = allFiles.filter((f) => /\.(css|ts|tsx)$/.test(f));
const TOKEN_SHEETS = [TOKEN_SHEET, ALT_SHEET, EXT_SHEET, EXT_ALT_SHEET];
const nonTokenFiles = styleAndCodeFiles.filter((f) => !TOKEN_SHEETS.includes(f));
const nonTokenCss = nonTokenFiles.filter((f) => f.endsWith(".css"));

const read = (f: string) => readFileSync(f, "utf-8");
const rel = (f: string) => relative(SRC, f).split(sep).join("/");

/** The declared value of a custom property in a given sheet. */
const valueOf = (sheet: string, name: string) => {
  const match = read(sheet).match(new RegExp(`${name}\\s*:\\s*([^;]+);`));
  expect(match, `${name} not found in ${rel(sheet)}`).not.toBeNull();
  return match![1].trim();
};

const NAMED_COLORS =
  /\b(?:white|black|red|blue|green|yellow|orange|purple|pink|brown|gray|grey|cyan|magenta|silver|gold|beige|ivory|teal|navy|maroon|olive|lime|aqua|fuchsia)\b/;

describe("token discipline", () => {
  // Check 1: no color literals outside the token sheets.
  it("confines color literals to the token sheet", () => {
    for (const file of nonTokenFiles) {
      const text = read(file);
      expect(text, `hex color in ${rel(file)}`).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(text, `rgb/hsl color in ${rel(file)}`).not.toMatch(/\b(?:rgba?|hsla?)\(/);
      if (file.endsWith(".css")) {
        // Scan the VALUE, not the whole declaration: property names legally
        // contain color words (white-space, border-*) and matching those is a
        // false positive, not a leak. `color: white` still fails.
        for (const line of text.split("\n")) {
          const value = line.match(/:\s*([^;]*)/)?.[1];
          if (value !== undefined) {
            expect(value, `named color in ${rel(file)}: ${line.trim()}`).not.toMatch(NAMED_COLORS);
          }
        }
      }
    }
  });

  // Check 2: spacing, radius, and shadow literals likewise confined.
  it("confines spacing, radius, and shadow literals to the token sheet", () => {
    // A bare 0 (margin: 0 resets) is not a themable value; only digits with
    // units count as literals.
    const propertyWithLiteral =
      /(?:^|\s)(margin|padding|gap|border-radius|box-shadow|font-size)\s*:\s*(?![^;]*var\(--eoe-)[^;]*\d+(?:\.\d+)?(?:px|rem|em|vh|vw|%)/;
    for (const file of nonTokenCss) {
      for (const line of read(file).split("\n")) {
        expect(line, `literal in ${rel(file)}: ${line.trim()}`).not.toMatch(propertyWithLiteral);
      }
    }
  });

  // Check 3: every referenced --eoe-* variable is defined in the sheet
  // (either the locked five-namespace sheet or the D21 additive extension).
  it("has a definition for every referenced token", () => {
    const defined = new Set(
      [...read(TOKEN_SHEET).matchAll(/--eoe-[a-z0-9-]+(?=\s*:)/g)].map((m) => m[0]),
    );
    for (const match of read(EXT_SHEET).matchAll(/--eoe-[a-z0-9-]+(?=\s*:)/g)) {
      defined.add(match[0]);
    }
    for (const file of nonTokenFiles) {
      for (const match of read(file).matchAll(/var\((--eoe-[a-z0-9-]+)/g)) {
        expect(defined.has(match[1]), `undefined token ${match[1]} in ${rel(file)}`).toBe(true);
      }
    }
  });

  // Check 4: all five binding namespaces are present and populated.
  it("populates all five token namespaces", () => {
    const sheet = read(TOKEN_SHEET);
    for (const namespace of ["color", "space", "font", "radius", "shadow"]) {
      const matches = sheet.match(new RegExp(`--eoe-${namespace}-[a-z0-9-]+\\s*:`, "g")) ?? [];
      expect(matches.length, `namespace --eoe-${namespace}-* is empty`).toBeGreaterThanOrEqual(2);
    }
  });

  // Check 5: tokens are defined only in the four known sheets, and the two
  // night sheets are pulled in by main.tsx alone. D24 shipped the night theme,
  // so "tokens.alt.css is referenced nowhere" no longer holds — the invariant
  // that replaces it is that no component may import a theme sheet directly.
  // Theme selection is lib/theme.ts flipping data-theme, full stop; a
  // component importing a sheet would reintroduce the ordering fragility the
  // attribute selector exists to avoid.
  it("defines tokens in exactly the known application sheets", () => {
    const definers = styleAndCodeFiles.filter((f) => /--eoe-[a-z0-9-]+\s*:/.test(read(f)));
    expect(definers.map(rel).sort()).toEqual([
      "styles/tokens.alt.css",
      "styles/tokens.css",
      "styles/tokens.ext.alt.css",
      "styles/tokens.ext.css",
    ]);
    // IMPORTS specifically — prose naming the sheets is documentation, and
    // lib/theme.ts has to explain what the attribute it sets is for.
    const importsAthemeSheet = /import\s+["'][^"']*tokens\.(ext\.)?alt\.css["']/;
    for (const file of nonTokenFiles) {
      if (file === THEME_ENTRYPOINT) {
        continue;
      }
      expect(read(file), `night sheet imported outside main.tsx: ${rel(file)}`).not.toMatch(
        importsAthemeSheet,
      );
    }
    expect(read(THEME_ENTRYPOINT)).toContain("tokens.alt.css");
    expect(read(THEME_ENTRYPOINT)).toContain("tokens.ext.alt.css");
  });

  // Check 8 (D24): the night theme must hold the same alias contract the light
  // theme does, or a dark badge and a dark map marker for the same state drift
  // apart. Mirrors check 7 across the alt pair.
  it("keeps the night theme's danger/success/warning in sync with its status aliases", () => {
    const pairs: [string, string][] = [
      ["--eoe-color-danger", "--eoe-color-status-alerting"],
      ["--eoe-color-success", "--eoe-color-status-healthy"],
      ["--eoe-color-warning", "--eoe-color-status-degraded"],
    ];
    for (const [locked, status] of pairs) {
      expect(
        valueOf(ALT_SHEET, locked),
        `${locked} (tokens.alt.css) has drifted from its alias ${status} (tokens.ext.alt.css)`,
      ).toBe(valueOf(EXT_ALT_SHEET, status));
    }
  });

  // Check 9 (D24): the night extension overrides values, it never introduces
  // keys. A key defined only there would resolve in dark and be undefined in
  // light — exactly the class of bug check 3 catches for components.
  it("keeps the night extension a strict subset of the extension it overrides", () => {
    const keys = (sheet: string) =>
      new Set([...read(sheet).matchAll(/--eoe-[a-z0-9-]+(?=\s*:)/g)].map((m) => m[0]));
    const base = keys(EXT_SHEET);
    for (const key of keys(EXT_ALT_SHEET)) {
      expect(base.has(key), `${key} is set in tokens.ext.alt.css but not in tokens.ext.css`).toBe(
        true,
      );
    }
  });

  // Check 10 (D24): both night sheets must be scoped to the data-theme
  // attribute. A stray bare :root in either would make the night theme win
  // permanently, by import order, in every browser.
  it("scopes both night sheets to the theme attribute", () => {
    for (const sheet of [ALT_SHEET, EXT_ALT_SHEET]) {
      const withoutComments = read(sheet).replace(/\/\*[\s\S]*?\*\//g, "");
      const selectors = [...withoutComments.matchAll(/([^{}]+)\{/g)].map((m) => m[1].trim());
      expect(selectors, `unscoped selector in ${rel(sheet)}`).toEqual([':root[data-theme="dark"]']);
    }
  });

  // Check 6: the alternate sheet mirrors the exact key set.
  it("keeps the alternate sheet's key set identical to the default", () => {
    const keys = (sheet: string) =>
      [...read(sheet).matchAll(/--eoe-[a-z0-9-]+(?=\s*:)/g)].map((m) => m[0]).sort();
    expect(keys(ALT_SHEET)).toEqual(keys(TOKEN_SHEET));
  });

  // Check 7 (D21): danger/success/warning are documented as aliases of
  // status-alerting/status-healthy/status-degraded, but the two sheets set
  // them as separately hard-coded hex values (a cross-sheet var() reference
  // isn't possible without coupling tokens.css to tokens.ext.css). Assert the
  // values stay equal so "can never drift apart" is enforced, not aspirational.
  it("keeps the locked danger/success/warning values in sync with their status aliases", () => {
    const pairs: [string, string][] = [
      ["--eoe-color-danger", "--eoe-color-status-alerting"],
      ["--eoe-color-success", "--eoe-color-status-healthy"],
      ["--eoe-color-warning", "--eoe-color-status-degraded"],
    ];
    for (const [locked, status] of pairs) {
      expect(
        valueOf(TOKEN_SHEET, locked),
        `${locked} (tokens.css) has drifted from its alias ${status} (tokens.ext.css)`,
      ).toBe(valueOf(EXT_SHEET, status));
    }
  });
});
