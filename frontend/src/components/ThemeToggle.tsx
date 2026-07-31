/**
 * Manual light/dark override (DES.7). Not optional per the DES handoff: field
 * staff read this outdoors in daylight, where the OS preference is wrong.
 */
import { useEffect, useState } from "react";

import { Theme, resolveTheme, setTheme } from "../lib/theme";

export function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme>("light");

  useEffect(() => {
    setThemeState(resolveTheme());
  }, []);

  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    setThemeState(next);
  }

  return (
    <button
      type="button"
      className="chrome-button"
      onClick={toggle}
      data-testid="theme-toggle"
      aria-pressed={theme === "dark"}
      title={theme === "dark" ? "Switch to day theme" : "Switch to night theme"}
    >
      <span aria-hidden="true">{theme === "dark" ? "☀" : "☾"}</span>
      <span className="chrome-button-label">{theme === "dark" ? "Day" : "Night"}</span>
    </button>
  );
}
