/**
 * RBAC helper (task E0.7): hides or disables actions by role.
 *
 * ROLE_PERMISSIONS mirrors backend/app/auth/rbac.py, the canonical map; the
 * parity test in tests/rbac.test.ts parses the Python source and fails the
 * gate on any divergence. Extend both sides together, deliberately.
 */
import { Me } from "./auth";

export type Role = "owner" | "deployment_operator" | "field_tech" | "viewer";

export type Permission =
  | "manage_users"
  | "view_audit"
  | "manage_devices"
  | "manage_config"
  | "manage_provisioning"
  | "view_provisioning"
  | "view_telemetry"
  | "view_status"
  // E5.2 (phase-5 fixed choice 9): manage is owner + operator only, view is
  // every role - the services API is write-only, so a read holds no secret.
  | "manage_services"
  | "view_services";

const ALL_PERMISSIONS: Permission[] = [
  "manage_users",
  "view_audit",
  "manage_devices",
  "manage_config",
  "manage_provisioning",
  "view_provisioning",
  "view_telemetry",
  "view_status",
  "manage_services",
  "view_services",
];

export const ROLE_PERMISSIONS: Record<Role, readonly Permission[]> = {
  owner: ALL_PERMISSIONS,
  deployment_operator: [
    "manage_devices",
    "manage_config",
    "manage_provisioning",
    "view_provisioning",
    "view_telemetry",
    "view_status",
    "manage_services",
    "view_services",
  ],
  field_tech: ["manage_provisioning", "view_provisioning", "view_status", "view_services"],
  viewer: ["view_provisioning", "view_telemetry", "view_status", "view_services"],
};

export interface Assignment {
  role: string;
  deployment_id: string | null;
}

/** Mirrors app.auth.rbac.has_permission: org-wide (null) grants everywhere;
 * a scoped grant applies only to its own deployment; an org-level check is
 * satisfied only by an org-wide grant. */
export function can(
  assignments: readonly Assignment[],
  permission: Permission,
  deploymentId: string | null = null,
): boolean {
  for (const assignment of assignments) {
    const permissions = ROLE_PERMISSIONS[assignment.role as Role];
    if (!permissions || !permissions.includes(permission)) {
      continue;
    }
    if (assignment.deployment_id === null) {
      return true;
    }
    if (deploymentId !== null && assignment.deployment_id === deploymentId) {
      return true;
    }
  }
  return false;
}

export function meCan(
  me: Me | null | undefined,
  permission: Permission,
  deploymentId: string | null = null,
): boolean {
  if (!me) {
    return false;
  }
  return can(me.assignments ?? [], permission, deploymentId);
}
