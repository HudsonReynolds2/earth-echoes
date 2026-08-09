/**
 * Shared hierarchy-tree data (task E2.7): InventoryLayout's three queries
 * and node/crumb builders extracted so /configuration renders the same tree
 * against its own routes at ZERO extra fetches - the query keys are
 * identical, so whichever layout loads first warms the other.
 */
import { useQuery } from "@tanstack/react-query";
import { matchPath } from "react-router-dom";

import { Crumb } from "../components/ContextBar";
import { TreeNode } from "../components/HierarchyTree";
import {
  Aggregator,
  Deployment,
  listDeployments,
  listOrganizations,
  listPods,
  Organization,
  Pod,
} from "./inventory";

export interface HierarchyRoutes {
  /** "/inventory" | "/configuration" - the tree root and crumb home. */
  root: string;
  deployment(id: string): string;
  pod(id: string): string;
  /** Inventory sends aggregator rows to their pod; configuration gives them
   * their own route (overrides are writable at aggregator level). */
  aggregator(aggregator: Aggregator, podId: string): string;
  listener(mac: string): string;
}

export interface HierarchyData {
  org: Organization | undefined;
  deployments: Deployment[];
  pods: Pod[];
  nodes: TreeNode[];
  crumbsFor(pathname: string): Crumb[];
  isLoading: boolean;
}

export function useHierarchyTree(routes: HierarchyRoutes): HierarchyData {
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
  const deploymentItems = deployments.data?.items ?? [];
  const podItems = pods.data?.items ?? [];

  const nodes: TreeNode[] = org
    ? [
        {
          id: org.id,
          kind: "organization",
          label: org.name,
          count: deployments.data?.total,
          to: routes.root,
          children: deploymentItems.map((deployment) => ({
            id: deployment.id,
            kind: "deployment" as const,
            label: deployment.name,
            count: deployment.pod_count,
            to: routes.deployment(deployment.id),
            children: podItems
              .filter((pod) => pod.deployment_id === deployment.id)
              .map((pod) => ({
                id: pod.id,
                kind: "pod" as const,
                label: pod.name,
                count: pod.listener_count,
                to: routes.pod(pod.id),
                children: pod.aggregator
                  ? [
                      {
                        id: pod.aggregator.id,
                        kind: "aggregator" as const,
                        label: pod.aggregator.aggregator_uuid,
                        count: pod.aggregator.listener_count,
                        to: routes.aggregator(pod.aggregator, pod.id),
                      },
                    ]
                  : [],
              })),
          })),
        },
      ]
    : [];

  function crumbsFor(pathname: string): Crumb[] {
    const crumbs: Crumb[] = [{ label: org?.name ?? "Organization", to: routes.root }];
    const depMatch = matchPath(`${routes.root}/deployments/:deploymentId`, pathname);
    const podMatch = matchPath(`${routes.root}/pods/:podId`, pathname);
    const aggMatch = matchPath(`${routes.root}/aggregators/:aggregatorId`, pathname);
    const listenerMatch = matchPath(`${routes.root}/listeners/:mac`, pathname);
    if (depMatch) {
      const deployment = deploymentItems.find((item) => item.id === depMatch.params.deploymentId);
      crumbs.push({ label: deployment?.name ?? "Deployment" });
    } else if (podMatch) {
      const pod = podItems.find((item) => item.id === podMatch.params.podId);
      const deployment = deploymentItems.find((item) => item.id === pod?.deployment_id);
      if (deployment) {
        crumbs.push({ label: deployment.name, to: routes.deployment(deployment.id) });
      }
      crumbs.push({ label: pod?.name ?? "Pod" });
    } else if (aggMatch) {
      const pod = podItems.find((item) => item.aggregator?.id === aggMatch.params.aggregatorId);
      const deployment = deploymentItems.find((item) => item.id === pod?.deployment_id);
      if (deployment) {
        crumbs.push({ label: deployment.name, to: routes.deployment(deployment.id) });
      }
      if (pod) {
        crumbs.push({ label: pod.name, to: routes.pod(pod.id) });
      }
      crumbs.push({ label: pod?.aggregator?.aggregator_uuid ?? "Aggregator" });
    } else if (listenerMatch) {
      crumbs.push({ label: decodeURIComponent(listenerMatch.params.mac ?? "Listener") });
    } else {
      crumbs[0] = { label: org?.name ?? "Organization" }; // index: final crumb, no link
    }
    return crumbs;
  }

  return {
    org,
    deployments: deploymentItems,
    pods: podItems,
    nodes,
    crumbsFor,
    isLoading: organizations.isLoading || deployments.isLoading || pods.isLoading,
  };
}
