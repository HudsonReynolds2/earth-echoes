/**
 * The honest "nothing here yet" panel. Used where a surface is designed and
 * routed but its data does not exist yet, so the page says which epic brings
 * it rather than showing invented rows.
 */
import { ReactNode } from "react";

export function EmptyState({
  title,
  children,
  testId,
}: {
  title: string;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <div className="empty-state" data-testid={testId}>
      <h2>{title}</h2>
      <p>{children}</p>
    </div>
  );
}
