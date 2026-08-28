import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";

export function Provisioning() {
  return (
    <div className="page">
      <PageHeader eyebrow="Field operations" title="Provisioning" />
      <EmptyState title="No bundles yet" testId="provisioning-empty">
        The bundle tracking board and the generation wizard arrive with E4. Services onboarding
        already shipped with E5 — find it under a deployment&apos;s Services page in Inventory.
      </EmptyState>
    </div>
  );
}
