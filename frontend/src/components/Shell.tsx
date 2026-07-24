/**
 * Base layout shell (task E0.4): sidebar navigation plus content area.
 * Styled exclusively through the token sheet.
 */
import { NavLink, Outlet } from "react-router-dom";

export function Shell() {
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
      </aside>
      <main className="shell-content" data-testid="shell-content">
        <Outlet />
      </main>
    </div>
  );
}
