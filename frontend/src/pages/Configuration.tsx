import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";

export function Configuration() {
  return (
    <div className="page">
      <PageHeader eyebrow="Configuration" title="Inheritance editor" />
      <EmptyState title="No configuration scopes yet" testId="configuration-empty">
        The inheritance editor and the bulk-edit preview arrive with E2, on top of the E1 hierarchy
        they resolve against.
      </EmptyState>
    </div>
  );
}
