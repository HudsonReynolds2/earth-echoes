/**
 * Gate 4 browser checks (task E0.4). The theme test is E0.4's literal
 * acceptance criterion ("swapping its values visibly restyles the shell") and
 * DES.7's entire premise: selecting the alternate token sheet must change
 * computed styles with zero code changes.
 *
 * D24 changed HOW this is exercised, not what it proves. The night sheets now
 * ship scoped to :root[data-theme="dark"], so injecting them with
 * addStyleTag no longer applies them — and no longer resembles what users do.
 * The test now drives the real toggle, which is a stronger check: it covers
 * the sheets, the attribute, and the control in one pass.
 */
import { expect, test } from "@playwright/test";

test("shell loads and paints in a real browser", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("shell-topbar")).toBeVisible();
  await expect(page.getByTestId("shell-content")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
});

test("the theme toggle visibly restyles the shell", async ({ page }) => {
  await page.goto("/");

  const readStyles = () =>
    page.evaluate(() => {
      const body = getComputedStyle(document.body);
      const topbar = getComputedStyle(document.querySelector('[data-testid="shell-topbar"]')!);
      return {
        theme: document.documentElement.dataset.theme,
        background: body.backgroundColor,
        color: body.color,
        topbarBackground: topbar.backgroundColor,
      };
    });

  const before = await readStyles();
  await page.getByTestId("theme-toggle").click();
  const after = await readStyles();

  expect(before.theme).toBe("light");
  expect(after.theme).toBe("dark");
  expect(after.background).not.toBe(before.background);
  expect(after.color).not.toBe(before.color);
  expect(after.topbarBackground).not.toBe(before.topbarBackground);
});

test("the manual theme override survives a reload", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("theme-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  // The override is the point: field staff read this outdoors in daylight,
  // where the OS preference is the wrong answer every time.
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("status colors are relit for the night theme", async ({ page }) => {
  await page.goto("/map");
  const healthy = page.locator('[data-status="healthy"]').first();

  const colorOf = () => healthy.evaluate((element) => getComputedStyle(element).color);
  const light = await colorOf();
  await page.getByTestId("theme-toggle").click();
  const dark = await colorOf();

  // D21 shipped with the dark status palette missing, which rendered
  // near-black status colors on a near-black surface. D24 closed that gap.
  expect(dark).not.toBe(light);
});
