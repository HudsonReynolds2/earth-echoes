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

export const server = setupServer(
  http.get("http://api.test/api/v1/health", () => HttpResponse.json(healthPayload)),
);
