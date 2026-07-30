/**
 * MSW node server (task E0.4; decision D2): lets the frontend build and test
 * with no backend at all. Handlers mirror the OpenAPI contract.
 */
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

export const healthPayload = {
  status: "ok",
  version: "0.0.0",
  build_sha: "msw-fixture",
  database: "ok",
};

export const mePayload = {
  id: "00000000-0000-0000-0000-000000000001",
  email: "owner@example.com",
  is_active: true,
  assignments: [{ role: "owner", deployment_id: null }],
};

export const adminUsers = {
  items: [
    {
      id: "00000000-0000-0000-0000-000000000001",
      email: "owner@example.com",
      is_active: true,
      created_at: "2026-07-24T00:00:00Z",
      assignments: [{ role: "owner", deployment_id: null }],
    },
    {
      id: "00000000-0000-0000-0000-000000000002",
      email: "watcher@example.com",
      is_active: false,
      created_at: "2026-07-24T00:00:00Z",
      assignments: [{ role: "viewer", deployment_id: null }],
    },
  ],
  total: 2,
  limit: 50,
  offset: 0,
};

export const server = setupServer(
  http.get("http://api.test/api/v1/health", () => HttpResponse.json(healthPayload)),
  http.get("http://api.test/api/v1/users", () => HttpResponse.json(adminUsers)),
  // Default: signed out; individual tests override with an authenticated me.
  http.get("http://api.test/api/v1/auth/me", () =>
    HttpResponse.json(
      { error: { code: "unauthorized", message: "", detail: null } },
      { status: 401 },
    ),
  ),
  http.post("http://api.test/api/v1/auth/login", () => HttpResponse.json(mePayload)),
  http.post("http://api.test/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),
);
