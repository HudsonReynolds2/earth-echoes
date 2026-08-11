import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// No dev proxy, by decision D2 (docs/DECISIONS.md): the frontend talks to the
// API cross-origin from day one, so CORS behavior is exercised in development.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    // The published dev-server port (PHASE0-2-02): a host-run `npm run dev`
    // answers on the same URL the compose stack publishes, so the guides can
    // name one address. Inside the container the Dockerfile's CMD passes
    // --port 5173 and compose maps 15173:5173, which is why this value moving
    // does not change the image.
    port: 15173,
    strictPort: true,
  },
});
