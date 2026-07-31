import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";

export function Overview() {
  return (
    <div className="page">
      <PageHeader eyebrow="Organization" title="Overview" />
      <EmptyState title="No deployments yet" testId="overview-empty">
        The deployment roll-up, service health, and the attention queue arrive with E1 and E6.
        Accounts, roles, and platform status are live now under Users and System.
      </EmptyState>
    </div>
  );
}
