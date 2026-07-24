/**
 * Gate 6 frontend checks (task E0.6): login page behavior and the shell's
 * session affordance, against MSW.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { mePayload, server } from "./msw-server";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

describe("login page", () => {
  it("renders the form", () => {
    renderAt("/login");
    expect(screen.getByTestId("login-form")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  it("shows an error on invalid credentials", async () => {
    server.use(
      http.post("http://api.test/api/v1/auth/login", () =>
        HttpResponse.json(
          { error: { code: "unauthorized", message: "invalid credentials", detail: null } },
          { status: 401 },
        ),
      ),
    );
    renderAt("/login");
    await userEvent.type(screen.getByLabelText("Email"), "owner@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByTestId("login-error")).toHaveTextContent("Invalid email or password");
  });

  it("navigates to the overview on success", async () => {
    renderAt("/login");
    await userEvent.type(screen.getByLabelText("Email"), "owner@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "right");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });
});

describe("shell session affordance", () => {
  it("offers sign-in when logged out", async () => {
    renderAt("/");
    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows the account and sign-out when logged in", async () => {
    server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(mePayload)));
    renderAt("/");
    expect(await screen.findByTestId("auth-email")).toHaveTextContent("owner@example.com");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });
});
