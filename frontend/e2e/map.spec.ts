/**
 * E6.1 in a real browser (spec 9.1; D188). jsdom cannot lay out a map, so the
 * engine's actual rendering is asserted here: Leaflet draws into the region,
 * the switcher and attribution are on screen, and the map chrome is relit in
 * the night theme.
 *
 * **No tile server is contacted.** The API is mocked with the `none` basemap,
 * and every request to a known tile host is recorded and fails the test.
 */
import { expect, test } from "@playwright/test";

const API = "http://localhost:9/api/v1";
const TILE_HOSTS = ["server.arcgisonline.com", "tile.openstreetmap.org", "tile.googleapis.com"];

test.beforeEach(async ({ page }) => {
  await page.route(`${API}/map/config`, (route) =>
    route.fulfill({
      json: {
        offline: true,
        default: "none",
        sources: [{ id: "none", url: null, bounds: null, api_key: null }],
      },
    }),
  );
});

test("the map renders in the region with its switcher, attribution and legend", async ({
  page,
}) => {
  const tileRequests: string[] = [];
  page.on("request", (request) => {
    if (TILE_HOSTS.some((host) => request.url().includes(host))) tileRequests.push(request.url());
  });

  await page.goto("/map");
  const view = page.getByTestId("map-view");
  await expect(view).toBeVisible();
  await expect(view).toHaveAttribute("data-basemap", "none");

  // Leaflet laid itself out: a sized container with its panes and controls.
  const box = await page.locator(".leaflet-container").boundingBox();
  expect(box?.width ?? 0).toBeGreaterThan(200);
  expect(box?.height ?? 0).toBeGreaterThan(200);
  await expect(page.locator(".leaflet-control-zoom")).toBeVisible();
  await expect(page.locator(".leaflet-control-attribution")).toContainText("Leaflet");
  await expect(page.getByTestId("basemap-switcher")).toBeVisible();
  await expect(page.getByTestId("status-legend")).toBeVisible();

  expect(tileRequests).toEqual([]);
});

test("the map chrome is relit for the night theme", async ({ page }) => {
  await page.goto("/map");
  const zoomIn = page.locator(".leaflet-control-zoom-in");
  await expect(zoomIn).toBeVisible();
  const read = () => zoomIn.evaluate((element) => getComputedStyle(element).backgroundColor);

  const light = await read();
  await page.getByTestId("theme-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const dark = await read();

  // Leaflet ships white controls; a white box on the night map is the bug.
  expect(dark).not.toBe(light);
  expect(dark).not.toBe("rgb(255, 255, 255)");
});
