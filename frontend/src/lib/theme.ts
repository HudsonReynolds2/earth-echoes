/**
 * Theme resolution (DES.7). Writes document.documentElement.dataset.theme,
 * which is the selector tokens.alt.css and tokens.ext.alt.css key off.
 *
 * A stored choice always wins and pins the theme; with nothing stored the OS
 * preference decides and keeps deciding as it changes through the day.
 */
export type Theme = "light" | "dark";

const STORAGE_KEY = "eoe-theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

function storedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    // Safari private mode and hardened browser profiles throw on access;
    // falling back to the OS preference is correct, not an error state.
    return null;
  }
}

function systemTheme(): Theme {
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

export function resolveTheme(): Theme {
  return storedTheme() ?? systemTheme();
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export function setTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Losing persistence is survivable; losing the theme switch is not.
  }
  applyTheme(theme);
}

export function initTheme(): void {
  applyTheme(resolveTheme());
  window.matchMedia(DARK_QUERY).addEventListener("change", (event) => {
    if (storedTheme() === null) {
      applyTheme(event.matches ? "dark" : "light");
    }
  });
}
