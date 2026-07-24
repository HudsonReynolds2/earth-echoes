/**
 * Gate 4 component checks (task E0.4): shell regions, routing including the
 * 404 view, and TanStack Query resolving against MSW with loading and error
 * states.
 */
import { QueryClient } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { server } from "./msw-server";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

describe("shell and routing", () => {
  it("renders sidebar and content regions", () => {
    renderAt("/");
    expect(screen.getByTestId("shell-sidebar")).toBeInTheDocument();
    expect(screen.getByTestId("shell-content")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });

  it("renders each declared route", async () => {
    renderAt("/");
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    cleanup();
    renderAt("/system");
    expect(await screen.findByRole("heading", { name: "System" })).toBeInTheDocument();
  });

  it("renders the 404 view for unknown routes", () => {
    renderAt("/no-such-page");
    expect(screen.getByTestId("not-found")).toBeInTheDocument();
  });
});

describe("query integration against MSW", () => {
  it("shows loading, then data from the mocked API", async () => {
    renderAt("/system");
    expect(screen.getByTestId("health-loading")).toBeInTheDocument();
    expect(await screen.findByTestId("health-data")).toBeInTheDocument();
    expect(screen.getByText("msw-fixture")).toBeInTheDocument();
  });

  it("renders the error state when the API fails", async () => {
    server.use(
      http.get("http://api.test/api/v1/health", () =>
        HttpResponse.json({ error: {} }, { status: 500 }),
      ),
    );
    renderAt("/system");
    expect(await screen.findByTestId("health-error")).toBeInTheDocument();
  });
});
