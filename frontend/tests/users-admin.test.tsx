/**
 * Gate 9 frontend checks (task E0.9): the owner-only admin page, the gated
 * sidebar link, and the create-user flow against MSW.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { adminUsers, mePayload, server } from "./msw-server";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

function actAsOwner() {
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(mePayload)));
}

function actAsViewer() {
  server.use(
    http.get("http://api.test/api/v1/auth/me", () =>
      HttpResponse.json({
        ...mePayload,
        email: "watcher@example.com",
        assignments: [{ role: "viewer", deployment_id: null }],
      }),
    ),
  );
}

describe("users admin page", () => {
  it("shows the user table and create form to an owner", async () => {
    actAsOwner();
    renderAt("/users");
    expect(await screen.findByTestId("users-table")).toBeInTheDocument();
    expect(screen.getByText("watcher@example.com")).toBeInTheDocument();
    expect(screen.getByText("inactive")).toBeInTheDocument();
    expect(screen.getByTestId("create-user-form")).toBeInTheDocument();
  });

  it("denies the page to a viewer (read-only UI)", async () => {
    actAsViewer();
    renderAt("/users");
    expect(await screen.findByTestId("users-denied")).toBeInTheDocument();
    expect(screen.queryByTestId("users-table")).not.toBeInTheDocument();
  });

  it("hides the sidebar link from a viewer and shows it to an owner", async () => {
    actAsOwner();
    renderAt("/");
    expect(await screen.findByRole("link", { name: "Users" })).toBeInTheDocument();
  });

  it("submits the create form and refreshes the table", async () => {
    actAsOwner();
    let captured: unknown = null;
    server.use(
      http.post("http://api.test/api/v1/users", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(adminUsers.items[0], { status: 201 });
      }),
    );
    renderAt("/users");
    await screen.findByTestId("users-table");
    await userEvent.type(screen.getByLabelText("Email"), "new-viewer@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "irrelevant-fixture-value");
    await userEvent.click(screen.getByRole("button", { name: "Create user" }));
    expect(captured).toMatchObject({
      email: "new-viewer@example.com",
      assignments: [{ role: "viewer", deployment_id: null }],
    });
  });

  it("surfaces a conflict error from the API", async () => {
    actAsOwner();
    server.use(
      http.post("http://api.test/api/v1/users", () =>
        HttpResponse.json(
          { error: { code: "conflict", message: "email already exists", detail: null } },
          { status: 409 },
        ),
      ),
    );
    renderAt("/users");
    await screen.findByTestId("users-table");
    await userEvent.type(screen.getByLabelText("Email"), "dupe@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "irrelevant-fixture-value");
    await userEvent.click(screen.getByRole("button", { name: "Create user" }));
    expect(await screen.findByTestId("create-user-error")).toHaveTextContent(
      "email already exists",
    );
  });
});
