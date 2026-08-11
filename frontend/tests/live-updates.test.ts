/**
 * Gate 50: the live-update socket (task E3.12).
 *
 * The hook's contract is that events are INVALIDATION SIGNALS, never data.
 * Postgres NOTIFY is best-effort, so a browser that reconnects has missed
 * whatever happened while it was away — patching a cache from an event body
 * would leave a screen confidently stale. These tests pin that: every path
 * ends in a refetch, and none of them writes cache data from a payload.
 */
import { QueryClient } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClientProvider } from "@tanstack/react-query";

import { useLiveUpdates } from "../src/lib/useLiveUpdates";

class FakeSocket {
  static last: FakeSocket | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSocket.last = this;
  }
  close() {
    this.closed = true;
  }
}

let client: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client }, children);
}

beforeEach(() => {
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  FakeSocket.last = null;
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useLiveUpdates", () => {
  it("opens no socket until there is a session", () => {
    renderHook(() => useLiveUpdates(false), { wrapper });
    expect(FakeSocket.last).toBeNull();
  });

  it("dials the API's ws:// origin, not a hardcoded host", () => {
    renderHook(() => useLiveUpdates(true), { wrapper });
    expect(FakeSocket.last?.url).toBe("ws://api.test/api/v1/ws");
  });

  it("refetches everything on open, because a reconnect has missed events", () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useLiveUpdates(true), { wrapper });
    FakeSocket.last?.onopen?.();
    // No key: the whole cache. Whatever was missed while the socket was down
    // is picked up in one round trip.
    expect(invalidate).toHaveBeenCalledWith();
  });

  it("turns a device_status event into a refetch, not a cache write", () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const setData = vi.spyOn(client, "setQueryData");
    renderHook(() => useLiveUpdates(true), { wrapper });
    invalidate.mockClear();

    FakeSocket.last?.onmessage?.({
      data: JSON.stringify({
        channel: "device_status",
        deployment_id: "d",
        entity_type: "aggregator",
        entity_id: "a",
        data: { online: false },
        at: "2026-08-10T12:00:00Z",
      }),
    });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["aggregators"] });
    expect(setData, "an event body must never become cache data").not.toHaveBeenCalled();
  });

  it("a reconciliation event refreshes the timeline", () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useLiveUpdates(true), { wrapper });
    invalidate.mockClear();

    FakeSocket.last?.onmessage?.({
      data: JSON.stringify({
        channel: "reconciliation",
        deployment_id: "d",
        entity_type: "aggregator",
        entity_id: "a",
        data: { to_state: "applied" },
        at: "2026-08-10T12:00:00Z",
      }),
    });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["timeline"] });
  });

  it("survives a payload it cannot parse", () => {
    renderHook(() => useLiveUpdates(true), { wrapper });
    expect(() => FakeSocket.last?.onmessage?.({ data: "not json" })).not.toThrow();
  });

  it("does not retry after a 1008 close", () => {
    vi.useFakeTimers();
    renderHook(() => useLiveUpdates(true), { wrapper });
    const first = FakeSocket.last;
    // 1008 is "you may not", not "try again": retrying would hammer the API
    // with a login the browser does not have.
    first?.onclose?.({ code: 1008 });
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.last).toBe(first);
    vi.useRealTimers();
  });

  it("reconnects with backoff after an unexpected close", () => {
    vi.useFakeTimers();
    renderHook(() => useLiveUpdates(true), { wrapper });
    const first = FakeSocket.last;
    first?.onclose?.({ code: 1006 });
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.last).not.toBe(first);
    vi.useRealTimers();
  });

  it("closes the socket when it unmounts", () => {
    const { unmount } = renderHook(() => useLiveUpdates(true), { wrapper });
    const socket = FakeSocket.last;
    unmount();
    expect(socket?.closed).toBe(true);
  });
});
