import { EmptyState } from "../components/EmptyState";

export function NotFound() {
  return (
    <div className="page">
      <EmptyState title="Not found" testId="not-found">
        No such page.
      </EmptyState>
    </div>
  );
}
