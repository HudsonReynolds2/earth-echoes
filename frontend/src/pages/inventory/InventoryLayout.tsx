/**
 * The inventory frame (task E1.8): the full-bleed page shape — ContextBar
 * first (its first real consumer, D25), then the S3 tree rail beside the
 * routed level. Since E2.7 the tree data and crumb builder live in the
 * shared useHierarchyTree hook (identical query keys — the extraction is
 * behavior-neutral and lets /configuration reuse the tree at zero extra
 * fetches); this layout keeps only its own route family and the bulk-import
 * crumb special case.
 */
import { useState } from "react";
import { matchPath, Outlet, useLocation } from "react-router-dom";

import { ContextBar } from "../../components/ContextBar";
import { HierarchyTree } from "../../components/HierarchyTree";
import { HierarchyRoutes, useHierarchyTree } from "../../lib/hierarchy";

export interface InventoryOutletContext {
  filter: string;
}

const INVENTORY_ROUTES: HierarchyRoutes = {
  root: "/inventory",
  deployment: (id) => `/inventory/deployments/${id}`,
  pod: (id) => `/inventory/pods/${id}`,
  // No separate aggregator route in inventory: one aggregator per pod.
  aggregator: (_aggregator, podId) => `/inventory/pods/${podId}`,
  listener: (mac) => `/inventory/listeners/${encodeURIComponent(mac)}`,
};

export function InventoryLayout() {
  const [filter, setFilter] = useState("");
  const location = useLocation();
  const tree = useHierarchyTree(INVENTORY_ROUTES);

  const importMatch = matchPath("/inventory/import", location.pathname);
  const crumbs = importMatch
    ? [{ label: tree.org?.name ?? "Organization", to: "/inventory" }, { label: "Bulk import" }]
    : tree.crumbsFor(location.pathname);

  return (
    <>
      <ContextBar crumbs={crumbs}>
        <input
          className="tree-filter"
          aria-label="Filter by name"
          placeholder="Filter by name"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
      </ContextBar>
      <div className="inventory-body">
        <HierarchyTree nodes={tree.nodes} />
        <div className="page inventory-main">
          <Outlet context={{ filter } satisfies InventoryOutletContext} />
        </div>
      </div>
    </>
  );
}
