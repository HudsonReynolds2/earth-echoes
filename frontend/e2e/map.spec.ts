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

// --- E6.2b: markers, drill-down, clusters, pins -----------------------------------

const DEP = "d1000000-0000-4000-8000-000000000001";
const DEP2 = "d2000000-0000-4000-8000-000000000002";
const POD_A = "b1100000-0000-4000-8000-000000000001";
const POD_B = "b1200000-0000-4000-8000-000000000002";
const ZERO = {
  alerting: 0,
  offline: 0,
  degraded: 0,
  drifted: 0,
  sleeping: 0,
  healthy: 0,
  unknown: 0,
};

function deploymentRow(id: string, name: string, lat: number, lon: number, status: string) {
  return {
    id,
    name,
    slug: name.toLowerCase().replace(/ /g, "-"),
    gps_lat: lat,
    gps_lon: lon,
    status,
    counts: { ...ZERO, [status]: 1 },
    alert_count: status === "alerting" ? 1 : 0,
    services_status: "verified",
    pod_count: 2,
    listener_count: 6,
  };
}

const LISTENERS = [0, 1, 2, 3, 4, 5].map((n) => ({
  mac: `02:EE:0E:01:01:0${n + 1}`,
  name: `pin-${n + 1}`,
  pod_id: n < 4 ? POD_A : POD_B,
  aggregator_id: n < 4 ? "agg-a" : "agg-b",
  gps_lat: (n < 4 ? 41.221 : 41.208) + (n % 2) * 0.0012,
  gps_lon: (n < 4 ? -124.012 : -123.992) + (n % 3) * 0.0016,
  status: n === 2 ? "alerting" : "healthy",
  alert_count: n === 2 ? 1 : 0,
}));

test.describe("markers", () => {
  test.beforeEach(async ({ page }) => {
    await page.route(`${API}/auth/me`, (route) =>
      route.fulfill({
        json: {
          id: "u1",
          email: "owner@example.com",
          is_active: true,
          assignments: [{ role: "owner", deployment_id: null }],
        },
      }),
    );
    await page.route(`${API}/map/deployments`, (route) =>
      route.fulfill({
        json: [
          deploymentRow(DEP, "Redwood Coast", 41.213, -124.004, "alerting"),
          deploymentRow(DEP2, "High Desert", 43.358, -121.056, "healthy"),
        ],
      }),
    );
    await page.route(`${API}/map/deployments/${DEP}`, (route) =>
      route.fulfill({
        json: {
          deployment: deploymentRow(DEP, "Redwood Coast", 41.213, -124.004, "alerting"),
          pods: [
            {
              id: POD_A,
              name: "Pod 01 · Alder Creek",
              aggregator_id: "agg-a",
              aggregator_uuid: "demo-agg-rc-01",
              aggregator_name: null,
              gps_lat: 41.221,
              gps_lon: -124.012,
              aggregator_status: "healthy",
              status: "alerting",
              counts: { ...ZERO, healthy: 4, alerting: 1 },
              alert_count: 1,
              listener_count: 4,
            },
            {
              id: POD_B,
              name: "Pod 02 · Ridge Line",
              aggregator_id: "agg-b",
              aggregator_uuid: "demo-agg-rc-02",
              aggregator_name: null,
              gps_lat: 41.208,
              gps_lon: -123.992,
              aggregator_status: "healthy",
              status: "healthy",
              counts: { ...ZERO, healthy: 3 },
              alert_count: 0,
              listener_count: 2,
            },
          ],
          listeners: LISTENERS,
        },
      }),
    );
  });

  test("drills from the organization to a deployment, and pins open from their cluster", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto("/map");
    const redwood = page.getByRole("button", { name: /^Deployment Redwood Coast, Alerting/ });
    await expect(redwood).toBeVisible();
    await expect(page.getByRole("button", { name: /^Deployment High Desert/ })).toBeVisible();

    await redwood.click();
    await expect(page).toHaveURL(new RegExp(`/map/deployments/${DEP}$`));
    await expect(page.getByRole("navigation", { name: "Hierarchy" })).toContainText(
      "Redwood Coast",
    );
    await expect(
      page.getByRole("button", { name: /^Pod Pod 01 · Alder Creek, Alerting/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: /^Pod Pod 02 · Ridge Line, Streaming/ }),
    ).toBeVisible();

    // Pins either sit in a cluster carrying the worst status inside it, or are
    // already apart at this zoom. Open any cluster until the alerting pin shows.
    const pin = page.getByRole("button", { name: /^Listener pin-3, Alerting, 1 alert/ });
    for (let attempt = 0; attempt < 4 && !(await pin.isVisible()); attempt += 1) {
      const cluster = page.locator(".map-cluster").first();
      if ((await cluster.count()) === 0) break;
      await expect(cluster).toHaveAttribute("data-status", /alerting|healthy/);
      await cluster.click();
      await page.waitForTimeout(600);
    }
    await expect(pin).toBeVisible();
    await pin.click();
    await expect(page).toHaveURL(
      /device=listener%3A02%3AEE%3A0E%3A01%3A01%3A03|device=listener:02:EE:0E:01:01:03/,
    );
    await expect(page.getByTestId("map-selected")).toContainText("pin-3");
  });

  test("markers are reachable from the keyboard", async ({ page }) => {
    await page.goto("/map");
    const redwood = page.getByRole("button", { name: /^Deployment Redwood Coast/ });
    await expect(redwood).toBeVisible();
    await redwood.focus();
    await expect(redwood).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`/map/deployments/${DEP}$`));
  });
});
