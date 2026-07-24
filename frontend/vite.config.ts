import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// No dev proxy, by decision D2 (docs/DECISIONS.md): the frontend talks to the
// API cross-origin from day one, so CORS behavior is exercised in development.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
  },
});
