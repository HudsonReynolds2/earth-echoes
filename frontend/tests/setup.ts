import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./msw-server";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  // RTL auto-cleanup needs vitest globals, which are off; without this,
  // rendered trees accumulate across tests and role queries hit duplicates.
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
