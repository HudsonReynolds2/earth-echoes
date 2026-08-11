/**
 * App shell (E0.4; auth E0.6; restructured to V2·S1 in DES.7, DECISIONS D25).
 * Dark top bar with horizontal nav, replacing the E0.4 left sidebar: the map
 * needs the full viewport width and the hierarchy breadcrumb needs a permanent
 * home the sidebar could not give it. Styled exclusively through the tokens.
 */
import { useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { logout } from "../lib/auth";
import { useLiveUpdates } from "../lib/useLiveUpdates";
import { useMe } from "../lib/useMe";
import { ThemeToggle } from "./ThemeToggle";

/* Primary nav lists every destination for every role. Hiding a whole section
 * of the product from the nav teaches an operator the wrong map of what exists
 * and makes a permission problem look like a missing feature; the pages
 * themselves gate their contents (see UsersAdmin), and the backend is the
 * authority regardless. */
const NAV: { to: string; label: string }[] = [
  { to: "/", label: "Overview" },
  { to: "/map", label: "Map" },
  { to: "/inventory", label: "Inventory" },
  { to: "/configuration", label: "Configuration" },
  { to: "/provisioning", label: "Provisioning" },
  { to: "/system", label: "System" },
  { to: "/users", label: "Users" },
];

function initials(email: string): string {
  const name = email.split("@")[0];
  const parts = name.split(/[._-]+/).filter(Boolean);
  const letters = parts.length >= 2 ? parts[0][0] + parts[1][0] : name.slice(0, 2);
  return letters.toUpperCase();
}

export function Shell() {
  const { data: me } = useMe();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  // One socket per tab, mounted here because every screen below wants the
  // same events (E3.12). Enabled only once there is a session to scope it:
  // an unauthenticated socket is closed by the server with 1008 anyway.
  useLiveUpdates(Boolean(me));

  async function onSignOut() {
    await logout();
    await queryClient.invalidateQueries({ queryKey: ["me"] });
    navigate("/login");
  }

  return (
    <div className="shell" data-testid="shell">
      <header className="shell-topbar" data-testid="shell-topbar">
        <div className="shell-brand">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">Echoes of Earth</span>
        </div>
        <nav className="shell-nav" aria-label="Primary">
          {NAV.map(({ to, label }) => (
            <NavLink key={to} to={to} end={to === "/"}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="shell-auth" data-testid="shell-auth">
          <ThemeToggle />
          {me ? (
            <>
              {/* The initials are a glance affordance; the email is the real
                  identifier, so it stays the accessible name and the tooltip
                  rather than being dropped from the shell entirely. */}
              <span
                className="avatar"
                data-testid="auth-email"
                role="img"
                aria-label={me.email}
                title={me.email}
              >
                {initials(me.email)}
              </span>
              <button type="button" className="chrome-button" onClick={onSignOut}>
                Sign out
              </button>
            </>
          ) : (
            <NavLink to="/login" className="chrome-button">
              Sign in
            </NavLink>
          )}
        </div>
      </header>
      <main className="shell-content" data-testid="shell-content">
        <Outlet />
      </main>
    </div>
  );
}
