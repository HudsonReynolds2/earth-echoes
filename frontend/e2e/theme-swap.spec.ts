/**
 * Gate 4 browser checks (task E0.4). The theme-swap test is E0.4's literal
 * acceptance criterion ("swapping its values visibly restyles the shell") and
 * DES.7's entire premise: loading the alternate token sheet must change
 * computed styles with zero code changes.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

const HERE = dirname(fileURLToPath(import.meta.url));
const ALT_SHEET = readFileSync(join(HERE, "..", "src", "styles", "tokens.alt.css"), "utf-8");

test("shell loads and paints in a real browser", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("shell-sidebar")).toBeVisible();
  await expect(page.getByTestId("shell-content")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
});

test("swapping token values visibly restyles the shell", async ({ page }) => {
  await page.goto("/");
  const shell = page.getByTestId("shell");

  const readStyles = () =>
    shell.evaluate((element) => {
      const body = getComputedStyle(document.body);
      const sidebar = getComputedStyle(
        element.querySelector('[data-testid="shell-sidebar"]') as Element,
      );
      return {
        background: body.backgroundColor,
        color: body.color,
        sidebarBackground: sidebar.backgroundColor,
        sidebarBorderColor: sidebar.borderRightColor,
      };
    });

  const before = await readStyles();
  await page.addStyleTag({ content: ALT_SHEET });
  const after = await readStyles();

  expect(after.background).not.toBe(before.background);
  expect(after.color).not.toBe(before.color);
  expect(after.sidebarBackground).not.toBe(before.sidebarBackground);
  expect(after.sidebarBorderColor).not.toBe(before.sidebarBorderColor);
});
