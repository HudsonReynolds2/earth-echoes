import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";

export function Inventory() {
  return (
    <div className="page">
      <PageHeader eyebrow="Hierarchy" title="Inventory" />
      <EmptyState title="No devices yet" testId="inventory-empty">
        Deployments, pods, listeners, and aggregators arrive with E1. Bulk import and the selection
        builder land alongside them.
      </EmptyState>
    </div>
  );
}
