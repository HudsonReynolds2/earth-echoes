/**
 * Gate 49: the device timeline panel (task E3.11; spec 6.3, 6.2).
 *
 * The panel renders history, and history has two properties a UI can quietly
 * ruin. A system-driven transition must not be attributed to a person —
 * `failed(timeout)` means NOBODY answered, and "unknown user" would suggest
 * the platform lost track of someone who was never there. And a revision
 * state must not be dressed as a device status: those are different
 * vocabularies (spec 6.2 vs 9.3), and D40's honesty guard still forbids
 * `[data-status]` on inventory routes until E3.12 has real status to show.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { actorLabel, diffRows, renderValue, triggerLabel } from "../src/lib/timeline";
import { FIXTURE_IDS } from "./inventory-fixture";
import { mePayload, server } from "./msw-server";

const POD_PATH = `/inventory/pods/${FIXTURE_IDS.alderCreekPod}`;

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

function entry(over: Partial<Record<string, unknown>> = {}) {
  return {
    id: crypto.randomUUID(),
    at: "2026-08-10T12:00:00Z",
    revision_id: "11111111-1111-1111-1111-111111111111",
    from_state: "draft",
    to_state: "pending",
    trigger: "publish",
    actor_user_id: "22222222-2222-2222-2222-222222222222",
    actor_email: "owner@example.com",
    diff: null,
    detail: null,
    ...over,
  };
}

function serveTimeline(items: unknown[]) {
  server.use(
    http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(mePayload)),
    http.get("http://api.test/api/v1/:entity/:id/timeline", () =>
      HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 }),
    ),
  );
}

beforeEach(() => {
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(mePayload)));
});

describe("pure helpers", () => {
  it("names every spec 6.2 trigger, and shows an unknown one verbatim", () => {
    expect(triggerLabel("timeout")).toBe("timed out with no reply");
    expect(triggerLabel("report_error")).toBe("rejected by the device");
    // A trigger this build has not learned means the backend grew an edge.
    // Guessing at it would be worse than showing the raw token.
    expect(triggerLabel("teleported")).toBe("teleported");
  });

  it("attributes a system transition to the platform, never to a person", () => {
    expect(actorLabel(entry({ actor_user_id: null, actor_email: null }) as never)).toBe(
      "the platform",
    );
    expect(actorLabel(entry({ actor_email: null }) as never)).toBe("a deleted user");
    expect(actorLabel(entry() as never)).toBe("owner@example.com");
  });

  it("orders diff rows stably and renders unset values as unset", () => {
    const rows = diffRows({ b: { before: 1, after: 2 }, a: { before: null, after: "x" } });
    expect(rows.map(([key]) => key)).toEqual(["a", "b"]);
    expect(renderValue(null)).toBe("unset");
    expect(renderValue(undefined)).toBe("unset");
    expect(renderValue(48000)).toBe("48000");
  });
});

describe("the panel", () => {
  it("says so plainly when a device has no history", async () => {
    serveTimeline([]);
    renderAt(POD_PATH);
    expect(await screen.findByTestId("timeline-empty")).toHaveTextContent(
      /no configuration has been published/i,
    );
  });

  it("renders the journey newest first with what moved each step", async () => {
    serveTimeline([
      entry({
        at: "2026-08-10T12:05:00Z",
        from_state: "pending",
        to_state: "failed",
        trigger: "timeout",
        actor_user_id: null,
        actor_email: null,
      }),
      entry({
        at: "2026-08-10T12:00:00Z",
        from_state: "draft",
        to_state: "pending",
        trigger: "publish",
      }),
    ]);
    renderAt(POD_PATH);

    const panel = await screen.findByTestId("device-timeline");
    const entries = within(panel).getAllByRole("listitem");
    expect(entries).toHaveLength(2);
    expect(entries[0]).toHaveTextContent("timed out with no reply");
    expect(entries[0]).toHaveTextContent("by the platform");
    expect(entries[1]).toHaveTextContent("published");
    expect(entries[1]).toHaveTextContent("owner@example.com");
  });

  it("shows the before/after config diff on the publish that carried it", async () => {
    serveTimeline([
      entry({
        diff: { "capture.sample_rate_hz": { before: 48000, after: 22050 } },
      }),
    ]);
    renderAt(POD_PATH);

    const panel = await screen.findByTestId("device-timeline");
    const row = within(panel).getByRole("row", { name: /capture\.sample_rate_hz/ });
    expect(row).toHaveTextContent("48000");
    expect(row).toHaveTextContent("22050");
  });

  it("names the keys a device disagreed on, and no values", async () => {
    serveTimeline([
      entry({
        from_state: "applied",
        to_state: "drifted",
        trigger: "report_diverged",
        actor_user_id: null,
        actor_email: null,
        detail: { differing_keys: ["logging.verbosity"], found_by: "drift_sweep" },
      }),
    ]);
    renderAt(POD_PATH);

    const panel = await screen.findByTestId("device-timeline");
    expect(panel).toHaveTextContent("Device disagreed on:");
    expect(panel).toHaveTextContent("logging.verbosity");
  });

  it("HONESTY GUARD: a revision state is not dressed as a device status", async () => {
    // D40 forbids [data-status] on inventory routes until E3.12 has real
    // device status. A spec 6.2 revision state is a different vocabulary and
    // must not borrow the attribute that guard watches.
    serveTimeline([entry({ to_state: "failed", trigger: "timeout" })]);
    renderAt(POD_PATH);

    const panel = await screen.findByTestId("device-timeline");
    expect(panel.querySelectorAll("[data-status]")).toHaveLength(0);
    expect(panel.querySelectorAll("[data-revision-state]")).toHaveLength(1);
  });
});
