/**
 * E6.2a: setting where a Deployment and a Pod sit on the map (spec 9.2; D189).
 * Both or neither, in range, sent as one PATCH; read-only for a viewer; the
 * demo fixture's positions are shown as the API returns them.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { HALF_PAIR_MESSAGE, parseCoordinates } from "../src/components/LocationCard";
import { deployments, FIXTURE_IDS, listeners } from "./inventory-fixture";
import { mePayload, server } from "./msw-server";

function actAs(role: "owner" | "viewer") {
  const me =
    role === "owner"
      ? mePayload
      : { ...mePayload, assignments: [{ role: "viewer", deployment_id: null }] };
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(me)));
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

describe("parseCoordinates", () => {
  it("accepts a pair, or neither to clear", () => {
    expect(parseCoordinates(" 41.213 ", "-124.004")).toEqual({
      gps_lat: 41.213,
      gps_lon: -124.004,
    });
    expect(parseCoordinates("", "")).toEqual({ gps_lat: null, gps_lon: null });
  });

  it("refuses half a pair, non-numbers and out-of-range values with the fix named", () => {
    expect(parseCoordinates("41.2", "")).toBe(HALF_PAIR_MESSAGE);
    expect(parseCoordinates("", "-124")).toBe(HALF_PAIR_MESSAGE);
    expect(parseCoordinates("north", "-124")).toMatch(/numbers/);
    expect(parseCoordinates("95", "0")).toMatch(/Latitude must be between/);
    expect(parseCoordinates("0", "181")).toMatch(/Longitude must be between/);
  });
});

describe("the fixture is placed", () => {
  it("puts every placed listener near its own pod, never on another pod's point", () => {
    const placed = listeners.filter((row) => row.gps_lat !== null);
    // Even indices per pod: 4 + 3 + 2 + 3 + 2 + 1.
    expect(placed).toHaveLength(15);
    const points = new Set(placed.map((row) => `${row.gps_lat},${row.gps_lon}`));
    expect(points.size).toBe(15);
    expect(listeners.find((row) => row.mac === FIXTURE_IDS.firstListenerMac)?.gps_lat).toBe(
      41.2222,
    );
  });
});

describe("deployment location", () => {
  it("shows the position and saves a new pair as one PATCH", async () => {
    actAs("owner");
    let body: unknown = null;
    server.use(
      http.patch(
        `http://api.test/api/v1/deployments/${FIXTURE_IDS.redwoodCoast}`,
        async ({ request }) => {
          body = await request.json();
          return HttpResponse.json({ ...deployments[0], ...(body as object) });
        },
      ),
    );
    renderAt(`/inventory/deployments/${FIXTURE_IDS.redwoodCoast}`);
    const card = await screen.findByTestId("deployment-location");
    expect(within(card).getByTestId("deployment-location-value")).toHaveTextContent(
      "41.213, -124.004",
    );
    await userEvent.click(within(card).getByRole("button", { name: "Edit location" }));
    const lat = within(card).getByLabelText("Latitude");
    await userEvent.clear(lat);
    await userEvent.type(lat, "41.5");
    await userEvent.click(within(card).getByRole("button", { name: "Save location" }));
    await screen.findByRole("button", { name: "Edit location" });
    expect(body).toEqual({ gps_lat: 41.5, gps_lon: -124.004 });
  });

  it("refuses half a pair before sending anything", async () => {
    actAs("owner");
    let sent = false;
    server.use(
      http.patch(`http://api.test/api/v1/deployments/${FIXTURE_IDS.redwoodCoast}`, () => {
        sent = true;
        return HttpResponse.json(deployments[0]);
      }),
    );
    renderAt(`/inventory/deployments/${FIXTURE_IDS.redwoodCoast}`);
    const card = await screen.findByTestId("deployment-location");
    await userEvent.click(within(card).getByRole("button", { name: "Edit location" }));
    await userEvent.clear(within(card).getByLabelText("Longitude"));
    await userEvent.click(within(card).getByRole("button", { name: "Save location" }));
    expect(within(card).getByRole("alert")).toHaveTextContent(HALF_PAIR_MESSAGE);
    expect(sent).toBe(false);
  });

  it("says plainly when a deployment is not placed", async () => {
    actAs("owner");
    server.use(
      http.get(`http://api.test/api/v1/deployments/${FIXTURE_IDS.redwoodCoast}`, () =>
        HttpResponse.json({ ...deployments[0], gps_lat: null, gps_lon: null }),
      ),
    );
    renderAt(`/inventory/deployments/${FIXTURE_IDS.redwoodCoast}`);
    const card = await screen.findByTestId("deployment-location");
    expect(card).toHaveTextContent("not drawn on the map");
    expect(within(card).getByRole("button", { name: "Set location" })).toBeInTheDocument();
  });

  it("is read-only for a viewer", async () => {
    actAs("viewer");
    renderAt(`/inventory/deployments/${FIXTURE_IDS.redwoodCoast}`);
    const card = await screen.findByTestId("deployment-location");
    expect(card).toHaveTextContent("41.213, -124.004");
    expect(within(card).queryByRole("button")).toBeNull();
  });
});

describe("pod location", () => {
  it("shows and edits the aggregator's position, which is the pod marker's", async () => {
    actAs("owner");
    let body: unknown = null;
    server.use(
      http.patch(
        `http://api.test/api/v1/aggregators/${FIXTURE_IDS.alderCreekAgg}`,
        async ({ request }) => {
          body = await request.json();
          return HttpResponse.json({});
        },
      ),
    );
    renderAt(`/inventory/pods/${FIXTURE_IDS.alderCreekPod}`);
    const card = await screen.findByTestId("pod-location");
    expect(card).toHaveTextContent("41.221, -124.012");
    await userEvent.click(within(card).getByRole("button", { name: "Edit location" }));
    await userEvent.clear(within(card).getByLabelText("Latitude"));
    await userEvent.clear(within(card).getByLabelText("Longitude"));
    await userEvent.click(within(card).getByRole("button", { name: "Save location" }));
    await screen.findByRole("button", { name: "Edit location" });
    expect(body).toEqual({ gps_lat: null, gps_lon: null });
  });
});
