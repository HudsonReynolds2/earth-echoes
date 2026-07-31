import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";

export function Provisioning() {
  return (
    <div className="page">
      <PageHeader eyebrow="Field operations" title="Provisioning" />
      <EmptyState title="No bundles yet" testId="provisioning-empty">
        The bundle tracking board and the generation wizard arrive with E4; the services onboarding
        wizard follows in E5.
      </EmptyState>
    </div>
  );
}
