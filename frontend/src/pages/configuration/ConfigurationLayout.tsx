/**
 * The configuration frame (task E2.7): same .inventory-body shape as
 * inventory, same tree at zero extra fetches (shared hook), its own route
 * family — including a real aggregator route, because overrides are
 * writable at aggregator level (unlike inventory, where the aggregator
 * lives on its pod's page). First real consumer of ContextBar's tab slot:
 * Settings / Tags / Revisions ride a ?tab= search param so deep links land
 * on the right pane. The S3 mockup's five-tab strip is folded to three —
 * Network is a settings group and secrets are rows, not a tab (recorded).
 */
import { Outlet, useLocation, useSearchParams } from "react-router-dom";

import { ContextBar } from "../../components/ContextBar";
import { HierarchyTree } from "../../components/HierarchyTree";
import { HierarchyRoutes, useHierarchyTree } from "../../lib/hierarchy";

export const CONFIG_ROUTES: HierarchyRoutes = {
  root: "/configuration",
  deployment: (id) => `/configuration/deployments/${id}`,
  pod: (id) => `/configuration/pods/${id}`,
  aggregator: (aggregator) => `/configuration/aggregators/${aggregator.id}`,
  listener: (mac) => `/configuration/listeners/${encodeURIComponent(mac)}`,
};

export const CONFIG_TABS = ["Settings", "Tags", "Revisions"] as const;
export type ConfigTab = (typeof CONFIG_TABS)[number];

export interface ConfigurationOutletContext {
  tab: ConfigTab;
}

export function ConfigurationLayout() {
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const tree = useHierarchyTree(CONFIG_ROUTES);
  const rawTab = params.get("tab");
  const tab: ConfigTab = CONFIG_TABS.includes(rawTab as ConfigTab)
    ? (rawTab as ConfigTab)
    : "Settings";

  return (
    <>
      <ContextBar
        crumbs={tree.crumbsFor(location.pathname)}
        tabs={[...CONFIG_TABS]}
        activeTab={tab}
        onTabChange={(next) =>
          setParams(next === "Settings" ? {} : { tab: next }, { replace: true })
        }
      />
      <div className="inventory-body">
        <HierarchyTree
          nodes={tree.nodes}
          testId="config-tree-rail"
          ariaLabel="Configuration tree"
        />
        <div className="page inventory-main">
          <Outlet context={{ tab } satisfies ConfigurationOutletContext} />
        </div>
      </div>
    </>
  );
}
