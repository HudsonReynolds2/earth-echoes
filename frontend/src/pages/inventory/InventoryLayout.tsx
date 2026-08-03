/**
 * The inventory frame (task E1.8): the full-bleed page shape — ContextBar
 * first (its first real consumer, D25), then the S3 tree rail beside the
 * routed level. Crumbs resolve from the same react-query keys the pages use,
 * so navigation costs no extra fetches once the tree is loaded.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { matchPath, Outlet, useLocation } from "react-router-dom";

import { ContextBar, Crumb } from "../../components/ContextBar";
import { HierarchyTree, TreeNode } from "../../components/HierarchyTree";
import { listDeployments, listOrganizations, listPods } from "../../lib/inventory";

export interface InventoryOutletContext {
  filter: string;
}

export function InventoryLayout() {
  const [filter, setFilter] = useState("");
  const location = useLocation();
  const organizations = useQuery({
    queryKey: ["organizations"],
    queryFn: () => listOrganizations(),
  });
  const deployments = useQuery({
    queryKey: ["deployments", "tree"],
    queryFn: () => listDeployments({ limit: 200 }),
  });
  const pods = useQuery({ queryKey: ["pods", "tree"], queryFn: () => listPods({ limit: 500 }) });

  const org = organizations.data?.items[0];
  const depMatch = matchPath("/inventory/deployments/:deploymentId", location.pathname);
  const podMatch = matchPath("/inventory/pods/:podId", location.pathname);
  const listenerMatch = matchPath("/inventory/listeners/:mac", location.pathname);
  const importMatch = matchPath("/inventory/import", location.pathname);

  const crumbs: Crumb[] = [{ label: org?.name ?? "Organization", to: "/inventory" }];
  if (depMatch) {
    const deployment = deployments.data?.items.find(
      (item) => item.id === depMatch.params.deploymentId,
    );
    crumbs.push({ label: deployment?.name ?? "Deployment" });
  } else if (podMatch) {
    const pod = pods.data?.items.find((item) => item.id === podMatch.params.podId);
    const deployment = deployments.data?.items.find((item) => item.id === pod?.deployment_id);
    if (deployment) {
      crumbs.push({
        label: deployment.name,
        to: `/inventory/deployments/${deployment.id}`,
      });
    }
    crumbs.push({ label: pod?.name ?? "Pod" });
  } else if (listenerMatch) {
    crumbs.push({ label: decodeURIComponent(listenerMatch.params.mac ?? "Listener") });
  } else if (importMatch) {
    crumbs.push({ label: "Bulk import" });
  } else {
    crumbs[0] = { label: org?.name ?? "Organization" }; // index: final crumb, no link
  }

  const nodes: TreeNode[] = org
    ? [
        {
          id: org.id,
          kind: "organization",
          label: org.name,
          count: deployments.data?.total,
          to: "/inventory",
          children: (deployments.data?.items ?? []).map((deployment) => ({
            id: deployment.id,
            kind: "deployment" as const,
            label: deployment.name,
            count: deployment.pod_count,
            to: `/inventory/deployments/${deployment.id}`,
            children: (pods.data?.items ?? [])
              .filter((pod) => pod.deployment_id === deployment.id)
              .map((pod) => ({
                id: pod.id,
                kind: "pod" as const,
                label: pod.name,
                count: pod.listener_count,
                to: `/inventory/pods/${pod.id}`,
                children: pod.aggregator
                  ? [
                      {
                        id: pod.aggregator.id,
                        kind: "aggregator" as const,
                        label: pod.aggregator.aggregator_uuid,
                        count: pod.aggregator.listener_count,
                        to: `/inventory/pods/${pod.id}`,
                      },
                    ]
                  : [],
              })),
          })),
        },
      ]
    : [];

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
        <HierarchyTree nodes={nodes} />
        <div className="page inventory-main">
          <Outlet context={{ filter } satisfies InventoryOutletContext} />
        </div>
      </div>
    </>
  );
}
