/**
 * E0.1 liveness placeholder: renders one element, exactly enough to prove the
 * container stack runs. Routing, TanStack Query, the layout shell, and the
 * design-token sheet are task E0.4 and MUST NOT appear before it
 * (phase-0-foundations.md section 4).
 */
import React from "react";
import { createRoot } from "react-dom/client";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("root element missing");
}
createRoot(root).render(
  <React.StrictMode>
    <p data-testid="eoe-placeholder">Echoes of Earth</p>
  </React.StrictMode>,
);
