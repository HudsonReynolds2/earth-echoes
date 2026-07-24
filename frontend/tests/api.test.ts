/**
 * Gate 4 API-client checks (task E0.4; decision D2): base URL comes from the
 * environment and its absence fails loudly.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiBaseUrl } from "../src/lib/api";

describe("api client", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("resolves its base URL from VITE_API_BASE_URL", () => {
    expect(apiBaseUrl()).toBe("http://api.test");
  });

  it("strips a trailing slash", () => {
    vi.stubEnv("VITE_API_BASE_URL", "http://api.test/");
    expect(apiBaseUrl()).toBe("http://api.test");
  });

  it("fails loudly when unset", () => {
    vi.stubEnv("VITE_API_BASE_URL", "");
    expect(() => apiBaseUrl()).toThrow(/VITE_API_BASE_URL/);
  });
});
