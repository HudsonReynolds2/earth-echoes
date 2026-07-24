/**
 * Base layout shell (task E0.4; auth affordance E0.6): sidebar navigation
 * plus content area. Styled exclusively through the token sheet.
 */
import { useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { logout } from "../lib/auth";
import { useMe } from "../lib/useMe";

export function Shell() {
  const { data: me } = useMe();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  async function onSignOut() {
    await logout();
    await queryClient.invalidateQueries({ queryKey: ["me"] });
    navigate("/login");
  }

  return (
    <div className="shell" data-testid="shell">
      <aside className="shell-sidebar" data-testid="shell-sidebar">
        <div className="shell-brand">Echoes of Earth</div>
        <nav className="shell-nav" aria-label="Primary">
          <NavLink to="/" end>
            Overview
          </NavLink>
          <NavLink to="/system">System</NavLink>
        </nav>
        <div className="shell-auth" data-testid="shell-auth">
          {me ? (
            <>
              <span className="muted" data-testid="auth-email">
                {me.email}
              </span>
              <button type="button" onClick={onSignOut}>
                Sign out
              </button>
            </>
          ) : (
            <NavLink to="/login">Sign in</NavLink>
          )}
        </div>
      </aside>
      <main className="shell-content" data-testid="shell-content">
        <Outlet />
      </main>
    </div>
  );
}
