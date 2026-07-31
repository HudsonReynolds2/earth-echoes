import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { initTheme } from "./lib/theme";
// Vendored @font-face declarations, first so the faces the token sheets name
// are registered before anything asks for them. No CDN (spec §15.1).
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/tokens.ext.css";
// Night theme. Both sheets are scoped to :root[data-theme="dark"], so they are
// inert until initTheme() sets the attribute; specificity, not import order,
// decides which values win.
import "./styles/tokens.alt.css";
import "./styles/tokens.ext.alt.css";
import "./styles/app.css";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("root element missing");
}
initTheme();
createRoot(root).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
