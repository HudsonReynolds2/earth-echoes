/**
 * V2·S2 context band: hierarchy breadcrumb plus a segmented tab control,
 * sitting directly under the top bar. This is the permanent home for the
 * breadcrumb that the E0.4 sidebar could not provide.
 *
 * Both slots are optional — a surface with hierarchy but no sub-views passes
 * crumbs only. Since E1.8 (its first real consumer) a crumb with `to` renders
 * a real router Link — additive change to the DES.7 contract, recorded in
 * DECISIONS; `to`-less crumbs (the Map page) are unaffected.
 */
import { ReactNode } from "react";
import { Link } from "react-router-dom";

export interface Crumb {
  label: string;
  to?: string;
}

export function ContextBar({
  crumbs,
  tabs,
  activeTab,
  onTabChange,
  children,
}: {
  crumbs: Crumb[];
  tabs?: string[];
  activeTab?: string;
  onTabChange?: (tab: string) => void;
  children?: ReactNode;
}) {
  return (
    <div className="context-bar" data-testid="context-bar">
      <nav className="breadcrumb" aria-label="Hierarchy">
        {crumbs.map((crumb, index) => (
          <span key={`${crumb.label}-${index}`}>
            {index > 0 && (
              <span className="breadcrumb-separator" aria-hidden="true">
                /
              </span>
            )}
            {crumb.to ? (
              <Link className="breadcrumb-link" to={crumb.to}>
                {crumb.label}
              </Link>
            ) : (
              <span className="breadcrumb-current" aria-current="page">
                {crumb.label}
              </span>
            )}
          </span>
        ))}
      </nav>
      {tabs && tabs.length > 0 && (
        <>
          <span className="context-divider" aria-hidden="true" />
          <div className="tab-group" role="tablist">
            {tabs.map((tab) => (
              <button
                key={tab}
                type="button"
                role="tab"
                aria-selected={tab === activeTab}
                className="tab"
                onClick={() => onTabChange?.(tab)}
              >
                {tab}
              </button>
            ))}
          </div>
        </>
      )}
      {children && <div className="context-bar-actions">{children}</div>}
    </div>
  );
}
