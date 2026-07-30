/**
 * V2 page header: uppercase mono eyebrow over a serif title, with an optional
 * action slot on the right. The serif is display-only (tokens.ext.css) — page
 * titles and the one hero metric per screen, never body or tables.
 */
import { ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="page-header-text">
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
      </div>
      {children && <div className="page-header-actions">{children}</div>}
    </header>
  );
}
