/**
 * Organization overview (task E1.8, project-changes #16): the V2·S1 layout
 * carrying ONLY E1-owned data — a real hero count and real deployment cards.
 * Telemetry volume, alerts, services, and status distributions belong to
 * E3/E5/E6/E7 and stay honest EmptyStates naming their epics; nothing here
 * is fabricated (no [data-status] renders on this page until E3).
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Can } from "../components/Can";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import { listDeployments, listListeners, listOrganizations, listPods } from "../lib/inventory";

export function Overview() {
  const organizations = useQuery({
    queryKey: ["organizations"],
    queryFn: () => listOrganizations(),
  });
  const deployments = useQuery({
    queryKey: ["deployments", "overview"],
    queryFn: () => listDeployments({ limit: 200 }),
  });
  // The D7 envelope makes counts free: limit=1, read total.
  const pods = useQuery({
    queryKey: ["pods", "count"],
    queryFn: () => listPods({ limit: 1 }),
  });
  const listeners = useQuery({
    queryKey: ["listeners", "count"],
    queryFn: () => listListeners({ limit: 1 }),
  });

  const org = organizations.data?.items[0];
  const deploymentRows = deployments.data?.items ?? [];

  if (deployments.isLoading) {
    return (
      <div className="page" data-testid="overview-loading">
        <div className="skeleton skeleton-row" />
        <div className="skeleton skeleton-row" />
        <p className="skeleton-caption">Querying deployments · skeleton holds final geometry</p>
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader eyebrow={org?.name ?? "Organization"} title="Organization overview">
        <div className="hero-metric" data-testid="overview-hero">
          <span className="eyebrow">Listeners registered</span>
          <span className="hero-number">{listeners.data?.total ?? 0}</span>
        </div>
        <Can permission="manage_devices">
          <Link className="btn-secondary" to="/inventory">
            Open inventory
          </Link>
        </Can>
      </PageHeader>
      <p className="scope-caption" data-testid="overview-meta">
        {deployments.data?.total ?? 0} deployments · {pods.data?.total ?? 0} pods ·{" "}
        {listeners.data?.total ?? 0} listeners
      </p>
      {deploymentRows.length === 0 ? (
        <EmptyState title="No deployments yet" testId="overview-empty">
          A deployment groups pods around one telemetry stack. Create one under Inventory, or bring
          an existing site in from a spreadsheet.{" "}
          <Link to="/inventory/import">Import inventory CSV</Link>
        </EmptyState>
      ) : (
        <div className="overview-grid">
          <div className="overview-deployments" data-testid="overview-deployments">
            <span className="eyebrow">Deployments</span>
            {deploymentRows.map((deployment) => (
              <section key={deployment.id} className="card">
                <h2>{deployment.name}</h2>
                <p className="muted">
                  <span className="mono">{deployment.slug}</span> · {deployment.pod_count} pods ·{" "}
                  {deployment.listener_count} listeners
                </p>
                <p className="muted">Device status arrives with E3 · services with E5.</p>
                <Link className="btn-secondary" to={`/inventory/deployments/${deployment.id}`}>
                  Open inventory
                </Link>
              </section>
            ))}
          </div>
          <div className="overview-attention">
            <span className="eyebrow">Needs attention</span>
            <EmptyState title="Nothing to triage yet" testId="overview-attention">
              Identity conflicts and reconciliation alerts surface here once E3 wires live reports;
              Grafana alerts follow with E7.
            </EmptyState>
          </div>
        </div>
      )}
    </div>
  );
}
