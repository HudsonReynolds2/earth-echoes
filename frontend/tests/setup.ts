import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./msw-server";

// jsdom implements no media queries at all, so lib/theme.ts's
// prefers-color-scheme probe throws rather than returning a value. Report "not
// dark", which is what a jsdom-equivalent browser with no stated preference
// would do; the real resolution path is covered in e2e/theme-swap.spec.ts.
window.matchMedia = (query: string) =>
  ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }) as MediaQueryList;

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  // RTL auto-cleanup needs vitest globals, which are off; without this,
  // rendered trees accumulate across tests and role queries hit duplicates.
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
