/**
 * E6.6: live map updates (spec 9.2, 13). The phase document's acceptance:
 *
 * - a `device_status` event leads to one refetch, the marker changes, and the
 *   map view and the open drawer (and its tab) are unchanged;
 * - a burst of 200 events produces at most one refetch per window.
 *
 * Driven through the real app with a fake socket, so the path under test is
 * the one a browser takes: event → `useLiveUpdates` → coalesced invalidation
 * → refetch → `MarkerLayer` updating the marker in place.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import L from "leaflet";
import { http, HttpResponse } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import { MAP_COALESCE_MS, createCoalescer } from "../src/lib/useLiveUpdates";
import { FIXTURE_IDS } from "./inventory-fixture";
import { mapDeploymentFixture } from "./map-fixture";
import { mePayload, server } from "./msw-server";

class FakeSocket {
  static last: FakeSocket | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.last = this;
  }
  close() {}
}

const LEAF_MAC = "02:EE:0E:01:01:02";
const POD = FIXTURE_IDS.alderCreekPod;

let lastLocation = "";
function LocationProbe() {
  const location = useLocation();
  lastLocation = decodeURIComponent(location.pathname + location.search);
  return null;
}

function event(n = 0) {
  return {
    data: JSON.stringify({
      channel: "device_status",
      deployment_id: FIXTURE_IDS.redwoodCoast,
      entity_type: "aggregator",
      entity_id: `agg-${n}`,
      data: { online: false },
      at: "2026-11-14T12:00:00Z",
    }),
  };
}

/** The Deployment's map read, counted, answering with the Alder Creek Pod
 * gone offline once `offline` is set. */
function countedDetail() {
  const state = { reads: 0, offline: false };
  server.use(
    http.get("http://api.test/api/v1/map/deployments/:id", ({ params }) => {
      state.reads += 1;
      const detail = structuredClone(mapDeploymentFixture(String(params.id))!);
      if (state.offline) {
        const pod = detail.pods.find((row) => row.id === POD)!;
        pod.status = "offline";
        pod.aggregator_status = "offline";
      }
      return HttpResponse.json(detail);
    }),
  );
  return state;
}

function marker(key: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-marker-key="${key}"]`);
}

async function settledSocket(): Promise<FakeSocket> {
  await waitFor(() => expect(FakeSocket.last?.onmessage).toBeTruthy());
  FakeSocket.last!.onopen?.();
  return FakeSocket.last!;
}

beforeEach(() => {
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  FakeSocket.last = null;
  // The socket opens only for a signed-in session (Shell).
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(mePayload)));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("the coalescer", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("runs at once, folds a burst into one trailing run, then goes quiet", () => {
    const action = vi.fn();
    const coalescer = createCoalescer(action, MAP_COALESCE_MS);
    for (let i = 0; i < 200; i += 1) coalescer.fire();
    expect(action).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(MAP_COALESCE_MS);
    expect(action).toHaveBeenCalledTimes(2); // the burst, once, after the window
    vi.advanceTimersByTime(MAP_COALESCE_MS * 5);
    expect(action).toHaveBeenCalledTimes(2); // nothing owed, nothing run
  });

  it("never runs twice inside one window, however the calls are spread", () => {
    const action = vi.fn();
    const coalescer = createCoalescer(action, MAP_COALESCE_MS);
    const runs: number[] = [];
    action.mockImplementation(() => runs.push(Date.now()));
    for (let t = 0; t < 10_000; t += 50) {
      coalescer.fire();
      vi.advanceTimersByTime(50);
    }
    vi.advanceTimersByTime(MAP_COALESCE_MS);
    for (let i = 1; i < runs.length; i += 1) {
      expect(runs[i] - runs[i - 1]).toBeGreaterThanOrEqual(MAP_COALESCE_MS);
    }
  });

  it("is cancelled with the socket, so nothing fires after unmount", () => {
    const action = vi.fn();
    const coalescer = createCoalescer(action, MAP_COALESCE_MS);
    coalescer.fire();
    coalescer.fire();
    coalescer.cancel();
    vi.advanceTimersByTime(MAP_COALESCE_MS * 2);
    expect(action).toHaveBeenCalledTimes(1);
  });
});

describe("a live update on the map", () => {
  it("changes the marker in place, with one refetch, and leaves the view and drawer alone", async () => {
    const state = countedDetail();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter
        initialEntries={[`/map/pods/${POD}?device=listener:${LEAF_MAC}&panel=timeline`]}
      >
        <App queryClient={client} />
        <LocationProbe />
      </MemoryRouter>,
    );
    const drawer = await screen.findByTestId("device-drawer");
    await waitFor(() => expect(marker(`pod:${POD}`)).not.toBeNull());
    const socket = await settledSocket();
    // The reconnect refetch is not the one under test; let it land.
    await waitFor(() => expect(state.reads).toBeGreaterThanOrEqual(1));
    await new Promise((resolve) => setTimeout(resolve, MAP_COALESCE_MS + 100));
    const podElement = marker(`pod:${POD}`)!;
    expect(podElement.querySelector("[data-status='alerting']")).not.toBeNull();

    const fit = vi.spyOn(L.Map.prototype, "fitBounds");
    const setView = vi.spyOn(L.Map.prototype, "setView");
    const before = state.reads;
    state.offline = true;
    socket.onmessage?.(event());

    await waitFor(() =>
      expect(marker(`pod:${POD}`)!.querySelector("[data-status='offline']")).not.toBeNull(),
    );
    expect(state.reads - before).toBe(1);
    // In place: the same element, not a rebuilt marker.
    expect(marker(`pod:${POD}`)).toBe(podElement);
    // The view did not move, and the drawer kept its device and its tab.
    expect(fit).not.toHaveBeenCalled();
    expect(setView).not.toHaveBeenCalled();
    expect(screen.getByTestId("device-drawer")).toBe(drawer);
    expect(within(drawer).getByRole("tab", { name: "Timeline" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(lastLocation).toContain(`device=listener:${LEAF_MAC}`);
    expect(lastLocation).toContain("panel=timeline");
    // It waits out one coalescing window on purpose, so it needs headroom
    // beyond vitest's 5 s default on a loaded runner.
  }, 15_000);

  it("turns a burst of 200 events into at most one refetch per window", async () => {
    const state = countedDetail();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter initialEntries={[`/map/deployments/${FIXTURE_IDS.redwoodCoast}`]}>
        <App queryClient={client} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(marker(`pod:${POD}`)).not.toBeNull());
    const socket = await settledSocket();
    await new Promise((resolve) => setTimeout(resolve, MAP_COALESCE_MS + 100));
    const before = state.reads;

    state.offline = true;
    for (let i = 0; i < 200; i += 1) socket.onmessage?.(event(i));
    // One at once, for the first event...
    await waitFor(() => expect(state.reads - before).toBe(1));
    // ...and the other 199 folded into one more at the window's end.
    await new Promise((resolve) => setTimeout(resolve, MAP_COALESCE_MS + 300));
    expect(state.reads - before).toBe(2);
    await new Promise((resolve) => setTimeout(resolve, MAP_COALESCE_MS + 100));
    expect(state.reads - before).toBe(2);
    expect(marker(`pod:${POD}`)!.querySelector("[data-status='offline']")).not.toBeNull();
  }, 15_000);

  it("never refetches the basemap configuration on a device event", async () => {
    let configReads = 0;
    server.use(
      http.get("http://api.test/api/v1/map/config", () => {
        configReads += 1;
        return HttpResponse.json({
          offline: true,
          default: "none",
          sources: [{ id: "none", url: null, bounds: null, api_key: null }],
        });
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter initialEntries={[`/map/deployments/${FIXTURE_IDS.redwoodCoast}`]}>
        <App queryClient={client} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(marker(`pod:${POD}`)).not.toBeNull());
    const socket = await settledSocket();
    await new Promise((resolve) => setTimeout(resolve, 200));
    const before = configReads;
    socket.onmessage?.(event());
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(configReads).toBe(before);
  });
});
