/**
 * Role gate (task E0.7): renders children only when the signed-in user holds
 * the permission. The disabled variant is for actions that should stay
 * visible but inert.
 */
import { ReactNode } from "react";

import { Permission, meCan } from "../lib/rbac";
import { useMe } from "../lib/useMe";

export function Can({
  permission,
  deploymentId = null,
  children,
  fallback = null,
}: {
  permission: Permission;
  deploymentId?: string | null;
  children: ReactNode;
  fallback?: ReactNode;
}) {
  const { data: me } = useMe();
  return <>{meCan(me, permission, deploymentId) ? children : fallback}</>;
}

export function useCan(permission: Permission, deploymentId: string | null = null): boolean {
  const { data: me } = useMe();
  return meCan(me, permission, deploymentId);
}
