/**
 * Auth client (task E0.6). Cookies ride credentials: "include" (D2); the
 * CSRF token is read from the JS-visible cookie and echoed in X-CSRF-Token
 * on mutations (double-submit, D4).
 */
import { apiBaseUrl } from "./api";

export interface Me {
  id: string;
  email: string;
  is_active: boolean;
  assignments: { role: string; deployment_id: string | null }[];
}

export function readCsrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)eoe_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export class TotpRequiredError extends Error {
  constructor() {
    super("Authentication code required");
  }
}

export async function login(email: string, password: string, totpCode?: string): Promise<Me> {
  const response = await fetch(`${apiBaseUrl()}/api/v1/auth/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, totp_code: totpCode ?? null }),
  });
  if (!response.ok) {
    if (response.status === 401) {
      try {
        const body = (await response.json()) as {
          error?: { detail?: { totp_required?: boolean } };
        };
        if (body.error?.detail?.totp_required) {
          throw new TotpRequiredError();
        }
      } catch (cause) {
        if (cause instanceof TotpRequiredError) {
          throw cause;
        }
        // fall through to the generic message on parse failure
      }
      throw new Error("Invalid email or password");
    }
    throw new Error("Login failed");
  }
  return (await response.json()) as Me;
}

export async function logout(): Promise<void> {
  const response = await fetch(`${apiBaseUrl()}/api/v1/auth/logout`, {
    method: "POST",
    credentials: "include",
    headers: { "X-CSRF-Token": readCsrfToken() },
  });
  if (!response.ok && response.status !== 401) {
    throw new Error("Logout failed");
  }
}

export async function fetchMe(): Promise<Me | null> {
  const response = await fetch(`${apiBaseUrl()}/api/v1/auth/me`, {
    credentials: "include",
  });
  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`auth/me failed: ${response.status}`);
  }
  return (await response.json()) as Me;
}
