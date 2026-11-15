/**
 * E6.1: the Map page (spec 9.1; D188). The engine renders inside DES.7's
 * frame, the switcher offers exactly what the server configured, the choice
 * is remembered, and a failed config read is said plainly.
 *
 * jsdom cannot lay out a map, so these assert structure and behavior. Real
 * rendering in both themes is `e2e/map.spec.ts`.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { mapConfigFixture, mePayload, server } from "./msw-server";

function actAsOwner() {
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(mePayload)));
}

function renderMap() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/map"]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

describe("map page", () => {
  afterEach(() => window.localStorage.clear());

  it("renders the map in the frame, satellite by default, legend kept", async () => {
    actAsOwner();
    renderMap();
    const view = await screen.findByTestId("map-view");
    expect(view).toHaveAttribute("data-basemap", "esri_imagery");
    expect(view.querySelector(".leaflet-container")).not.toBeNull();
    expect(screen.getByTestId("status-legend")).toBeInTheDocument();
    // E6.2b replaced E6.1's interim note with real markers; the fixture
    // places two Deployments, so no "nothing placed" note shows.
    expect(screen.queryByTestId("map-no-devices")).toBeNull();
    expect(screen.queryByTestId("map-empty")).toBeNull();
  });

  it("offers exactly the configured basemaps, with no Google entry and no key", async () => {
    actAsOwner();
    renderMap();
    const switcher = await screen.findByTestId("basemap-switcher");
    const options = within(switcher)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options).toEqual(["Satellite (Esri)", "Street (OpenStreetMap)", "No basemap"]);
  });

  it("remembers the viewer's choice across a remount", async () => {
    actAsOwner();
    const first = renderMap();
    await userEvent.selectOptions(await screen.findByRole("combobox"), "osm");
    expect(screen.getByTestId("map-view")).toHaveAttribute("data-basemap", "osm");
    first.unmount();

    renderMap();
    expect(await screen.findByTestId("map-view")).toHaveAttribute("data-basemap", "osm");
  });

  it("opens on the server's offline default when online sources are gone", async () => {
    actAsOwner();
    window.localStorage.setItem("eoe.map.basemap", "esri_imagery");
    server.use(
      http.get("http://api.test/api/v1/map/config", () =>
        HttpResponse.json({
          ...mapConfigFixture,
          offline: true,
          default: "none",
          sources: [{ id: "none", url: null, bounds: null, api_key: null }],
        }),
      ),
    );
    renderMap();
    expect(await screen.findByTestId("map-view")).toHaveAttribute("data-basemap", "none");
  });

  it("says so when the configuration cannot be read", async () => {
    actAsOwner();
    server.use(
      http.get("http://api.test/api/v1/map/config", () =>
        HttpResponse.json({ error: { code: "forbidden", message: "nope" } }, { status: 403 }),
      ),
    );
    renderMap();
    expect(await screen.findByTestId("map-config-error")).toHaveTextContent("nope");
    expect(screen.getByTestId("status-legend")).toBeInTheDocument();
  });
});
