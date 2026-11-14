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

const LEAVES = [1, 2, 3].map((n) => ({
  mac: `02:EE:0E:01:01:A${n}`,
  name: `leaf-${n}`,
  pod_id: POD_A,
  aggregator_id: "agg-a",
  gps_lat: null,
  gps_lon: null,
  status: "sleeping",
  alert_count: 0,
}));

/** E6.5: what `GET /map/devices/listener/{mac}` answers for any of them. */
function deviceRow(mac: string, name: string) {
  return {
    entity_type: "listener",
    id: mac,
    name,
    status: "sleeping",
    deployment_id: DEP,
    deployment_name: "Redwood Coast",
    pod_id: POD_A,
    pod_name: "Pod 01 · Alder Creek",
    gps_lat: null,
    gps_lon: null,
    alerts: [],
    revision_state: "applied",
    liveness: {
      state: "sleeping",
      last_audio_at: "2026-11-14T11:58:04Z",
      expected_wake_at: "2026-11-14T12:05:00Z",
      changed_at: "2026-11-14T11:58:30Z",
      reported_at: "2026-11-14T12:00:00Z",
      grace: { seconds: 30, source: "deployment", source_entity_id: DEP },
    },
    aggregator: {
      id: "agg-a",
      name: null,
      aggregator_uuid: "demo-agg-rc-01",
      status: "healthy",
      online: true,
      online_changed_at: "2026-11-14T03:00:00Z",
      revision_state: "applied",
    },
    counts: null,
    listener_count: null,
    pod_count: null,
    services_status: null,
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
    await page.route(`${API}/map/devices/listener/**`, (route) => {
      const mac = decodeURIComponent(route.request().url().split("/").pop()!);
      route.fulfill({ json: deviceRow(mac, `pin-${Number(mac.slice(-1))}`) });
    });
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
          listeners: [...LISTENERS, ...LEAVES],
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
    // E6.5: the selection opens the device drawer (E6.2b's interim card is gone).
    await expect(page.getByTestId("device-drawer")).toContainText("pin-3");
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

test.describe("leaves", () => {
  test("a Pod's GPS-less Listeners fan out around it on dashed tethers", async ({ page }) => {
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
    await page.route(`${API}/pods/${POD_A}`, (route) =>
      route.fulfill({ json: { id: POD_A, deployment_id: DEP, name: "Pod 01 · Alder Creek" } }),
    );
    await page.route(`${API}/map/devices/listener/**`, (route) => {
      const mac = decodeURIComponent(route.request().url().split("/").pop()!);
      route.fulfill({ json: deviceRow(mac, `leaf-${mac.slice(-1)}`) });
    });
    await page.route(`${API}/map/deployments/${DEP}`, (route) =>
      route.fulfill({
        json: {
          deployment: deploymentRow(DEP, "Redwood Coast", 41.213, -124.004, "healthy"),
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
              status: "sleeping",
              counts: { ...ZERO, healthy: 1, sleeping: 3 },
              alert_count: 0,
              listener_count: 3,
            },
          ],
          listeners: LEAVES,
        },
      }),
    );
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto(`/map/pods/${POD_A}`);
    const leaves = page.getByRole("button", { name: /no GPS, drawn beside its pod$/ });
    await expect(leaves).toHaveCount(3);
    await expect(page.locator(".map-marker-leaf .map-tether")).toHaveCount(3);
    await expect(page.getByText("3 no GPS")).toBeVisible();
    // Each leaf sits within a short tether of its Pod in real layout, once the
    // zoom has settled. Polling for NEARNESS, not just separation: a leaf
    // projected at the wrong zoom is still "apart", thousands of px away.
    const pod = page.getByRole("button", { name: /^Pod Pod 01 · Alder Creek/ });
    const centre = async (handle: typeof pod) => {
      const box = (await handle.boundingBox())!;
      return [box.x + box.width / 2, box.y + box.height / 2];
    };
    await expect
      .poll(async () => {
        const [px, py] = await centre(pod);
        const centres = await leaves.evaluateAll((elements) =>
          elements.map((element) => {
            const box = element.getBoundingClientRect();
            return [box.x + box.width / 2, box.y + box.height / 2];
          }),
        );
        return Math.max(...centres.map(([x, y]) => Math.hypot(x - px, y - py)));
      })
      .toBeLessThan(100);
    const centres = await leaves.evaluateAll((elements) =>
      elements.map((element) => {
        const box = element.getBoundingClientRect();
        return [box.x + box.width / 2, box.y + box.height / 2];
      }),
    );
    for (let a = 0; a < centres.length; a += 1) {
      for (let b = a + 1; b < centres.length; b += 1) {
        expect(
          Math.hypot(centres[a][0] - centres[b][0], centres[a][1] - centres[b][1]),
        ).toBeGreaterThan(20);
      }
    }
    if (process.env.EOE_SHOT) await page.screenshot({ path: process.env.EOE_SHOT });
  });
});

test.describe("the device drawer", () => {
  test("joins the selected marker to the drawer, dims the map, and hands focus back on Esc", async ({
    page,
  }) => {
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
    await page.route(`${API}/pods/${POD_A}`, (route) =>
      route.fulfill({ json: { id: POD_A, deployment_id: DEP, name: "Pod 01 · Alder Creek" } }),
    );
    await page.route(`${API}/map/devices/listener/**`, (route) => {
      const mac = decodeURIComponent(route.request().url().split("/").pop()!);
      route.fulfill({ json: deviceRow(mac, `leaf-${mac.slice(-1)}`) });
    });
    await page.route(`${API}/map/deployments/${DEP}`, (route) =>
      route.fulfill({
        json: {
          deployment: deploymentRow(DEP, "Redwood Coast", 41.213, -124.004, "healthy"),
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
              status: "sleeping",
              counts: { ...ZERO, healthy: 1, sleeping: 3 },
              alert_count: 0,
              listener_count: 3,
            },
          ],
          listeners: LEAVES,
        },
      }),
    );
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto(`/map/pods/${POD_A}`);
    const leaf = page.getByRole("button", { name: /^Listener leaf-2,/ });
    await expect(leaf).toBeVisible();
    await leaf.click();

    const drawer = page.getByTestId("device-drawer");
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("leaf-2");
    await expect(drawer).toContainText("2026-11-14 12:05:00Z");
    await expect(page.getByTestId("device-drawer-title")).toBeFocused();
    await expect(page.getByTestId("map-view")).toHaveAttribute("data-drawer-open", "true");
    await expect(leaf.locator(".map-marker-selected")).toHaveCount(1);

    // The leader line runs from the leaf's centre to the drawer's left edge.
    const line = page.getByTestId("map-leader").locator("line");
    await expect(line).toHaveCount(1);
    await expect
      .poll(async () => {
        const leafBox = (await leaf.boundingBox())!;
        const drawerBox = (await drawer.boundingBox())!;
        const viewBox = (await page.getByTestId("map-view").boundingBox())!;
        const [x1, y1, x2] = await line.evaluate((element) =>
          ["x1", "y1", "x2"].map((name) => Number(element.getAttribute(name))),
        );
        return [
          Math.round(Math.abs(viewBox.x + x1 - (leafBox.x + leafBox.width / 2))),
          Math.round(Math.abs(viewBox.y + y1 - (leafBox.y + leafBox.height / 2))),
          Math.round(Math.abs(viewBox.x + x2 - drawerBox.x)),
        ];
      })
      .toEqual([0, 0, 0]);

    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
    await expect(page.getByTestId("map-leader")).toHaveCount(0);
    await expect(leaf).toBeFocused();
  });
});
