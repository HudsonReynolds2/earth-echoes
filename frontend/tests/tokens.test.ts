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

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

const allFiles = walk(SRC);
const styleAndCodeFiles = allFiles.filter((f) => /\.(css|ts|tsx)$/.test(f));
const nonTokenFiles = styleAndCodeFiles.filter((f) => f !== TOKEN_SHEET && f !== ALT_SHEET);
const nonTokenCss = nonTokenFiles.filter((f) => f.endsWith(".css"));

const read = (f: string) => readFileSync(f, "utf-8");
const rel = (f: string) => relative(SRC, f).split(sep).join("/");

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
        for (const line of text.split("\n")) {
          if (/:\s*[^;]*$/.test(line) || line.includes(":")) {
            expect(line, `named color in ${rel(file)}: ${line.trim()}`).not.toMatch(NAMED_COLORS);
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

  // Check 3: every referenced --eoe-* variable is defined in the sheet.
  it("has a definition for every referenced token", () => {
    const defined = new Set(
      [...read(TOKEN_SHEET).matchAll(/--eoe-[a-z0-9-]+(?=\s*:)/g)].map((m) => m[0]),
    );
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

  // Check 5: the sheet lives in exactly one file; the alt sheet is test-only.
  it("defines tokens in exactly one application sheet", () => {
    const definers = styleAndCodeFiles.filter((f) => /--eoe-[a-z0-9-]+\s*:/.test(read(f)));
    expect(definers.map(rel).sort()).toEqual(["styles/tokens.alt.css", "styles/tokens.css"]);
    for (const file of nonTokenFiles) {
      expect(read(file), `tokens.alt.css referenced by app code ${rel(file)}`).not.toContain(
        "tokens.alt",
      );
    }
  });

  // Check 6: the alternate sheet mirrors the exact key set.
  it("keeps the alternate sheet's key set identical to the default", () => {
    const keys = (sheet: string) =>
      [...read(sheet).matchAll(/--eoe-[a-z0-9-]+(?=\s*:)/g)].map((m) => m[0]).sort();
    expect(keys(ALT_SHEET)).toEqual(keys(TOKEN_SHEET));
  });
});
