import { defineConfig } from "@playwright/test";

// Real-browser checks (task E0.4). Required, not optional: the theme-swap
// acceptance criterion needs computed styles, which jsdom cannot resolve for
// CSS custom properties (decision D6).
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  use: {
    baseURL: "http://localhost:5174",
  },
  webServer: {
    command: "npx vite --port 5174 --strictPort",
    url: "http://localhost:5174",
    reuseExistingServer: false,
    env: {
      // Deliberately dead: e2e asserts the shell frame, not live API data.
      VITE_API_BASE_URL: "http://localhost:9",
    },
  },
});
