/**
 * User administration client (task E0.9). Mutations echo the CSRF cookie in
 * X-CSRF-Token (D4).
 */
import { apiBaseUrl } from "./api";
import { readCsrfToken } from "./auth";

export interface AdminAssignment {
  role: string;
  deployment_id: string | null;
}

export interface AdminUser {
  id: string;
  email: string;
  is_active: boolean;
  created_at: string;
  assignments: AdminAssignment[];
}

export interface UserList {
  items: AdminUser[];
  total: number;
  limit: number;
  offset: number;
}

async function parseError(response: Response): Promise<never> {
  let message = `request failed: ${response.status}`;
  try {
    const body = (await response.json()) as { error?: { message?: string } };
    if (body.error?.message) {
      message = body.error.message;
    }
  } catch {
    // keep the status-based message
  }
  throw new Error(message);
}

export async function listUsers(): Promise<UserList> {
  const response = await fetch(`${apiBaseUrl()}/api/v1/users?sort=email`, {
    credentials: "include",
  });
  if (!response.ok) {
    return parseError(response);
  }
  return (await response.json()) as UserList;
}

export async function createUser(input: {
  email: string;
  password: string;
  role: string;
  deployment_id: string | null;
}): Promise<AdminUser> {
  const response = await fetch(`${apiBaseUrl()}/api/v1/users`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": readCsrfToken() },
    body: JSON.stringify({
      email: input.email,
      password: input.password,
      assignments: [{ role: input.role, deployment_id: input.deployment_id }],
    }),
  });
  if (!response.ok) {
    return parseError(response);
  }
  return (await response.json()) as AdminUser;
}

export async function setUserActive(id: string, isActive: boolean): Promise<AdminUser> {
  const response = await fetch(`${apiBaseUrl()}/api/v1/users/${id}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": readCsrfToken() },
    body: JSON.stringify({ is_active: isActive }),
  });
  if (!response.ok) {
    return parseError(response);
  }
  return (await response.json()) as AdminUser;
}
