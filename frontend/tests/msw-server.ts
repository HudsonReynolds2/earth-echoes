/**
 * MSW node server (task E0.4; decision D2): lets the frontend build and test
 * with no backend at all. Handlers mirror the OpenAPI contract.
 */
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

import { CATALOG_FIXTURE, fixtureEffective, fixtureOverrides } from "./config-fixture";
import {
  ORG,
  aggregators,
  applyListParams,
  deployments,
  listeners,
  pods,
} from "./inventory-fixture";

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
  // --- E1.8 inventory reads (global: every page render needs them; writes
  // are per-test server.use overrides, the users-admin pattern) ------------
  http.get("http://api.test/api/v1/organizations", ({ request }) =>
    HttpResponse.json(applyListParams([ORG], new URL(request.url))),
  ),
  http.get("http://api.test/api/v1/deployments", ({ request }) =>
    HttpResponse.json(applyListParams(deployments, new URL(request.url))),
  ),
  http.get("http://api.test/api/v1/pods", ({ request }) =>
    HttpResponse.json(applyListParams(pods, new URL(request.url))),
  ),
  http.get("http://api.test/api/v1/aggregators", ({ request }) =>
    HttpResponse.json(applyListParams(aggregators, new URL(request.url))),
  ),
  http.get("http://api.test/api/v1/listeners", ({ request }) =>
    HttpResponse.json(applyListParams(listeners, new URL(request.url))),
  ),
  http.get("http://api.test/api/v1/deployments/:id", ({ params }) => {
    const row = deployments.find((d) => d.id === params.id);
    return row
      ? HttpResponse.json(row)
      : HttpResponse.json(
          { error: { code: "not_found", message: "deployment not found", detail: null } },
          { status: 404 },
        );
  }),
  http.get("http://api.test/api/v1/pods/:id", ({ params }) => {
    const row = pods.find((p) => p.id === params.id);
    return row
      ? HttpResponse.json(row)
      : HttpResponse.json(
          { error: { code: "not_found", message: "pod not found", detail: null } },
          { status: 404 },
        );
  }),
  http.get("http://api.test/api/v1/listeners/:mac", ({ params }) => {
    const mac = decodeURIComponent(String(params.mac)).toUpperCase();
    const row = listeners.find((l) => l.mac === mac);
    return row
      ? HttpResponse.json(row)
      : HttpResponse.json(
          { error: { code: "not_found", message: "listener not found", detail: null } },
          { status: 404 },
        );
  }),
  // --- E2.7 config reads (global — every config page render needs them;
  // PUT overrides and preview/apply are per-test server.use overrides) -----
  http.get("http://api.test/api/v1/config/catalog", () => HttpResponse.json(CATALOG_FIXTURE)),
  http.get("http://api.test/api/v1/:entity/:id/config/effective", ({ params }) =>
    HttpResponse.json(
      fixtureEffective(String(params.entity), decodeURIComponent(String(params.id))),
    ),
  ),
  http.get("http://api.test/api/v1/:entity/:id/config/overrides", ({ params }) =>
    HttpResponse.json(
      fixtureOverrides(String(params.entity), decodeURIComponent(String(params.id))),
    ),
  ),
  http.get("http://api.test/api/v1/:entity/:id/revisions", () =>
    HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 }),
  ),
  http.get("http://api.test/api/v1/selections", () =>
    HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 }),
  ),
);
