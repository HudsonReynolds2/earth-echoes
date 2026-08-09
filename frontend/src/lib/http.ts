/**
 * Shared request plumbing (task E2.7), extracted verbatim from inventory.ts
 * so lib/config.ts and lib/inventory.ts ride one implementation: envelope-
 * aware error parsing into the typed ApiError, CSRF header on writes, and
 * the D7 query-string helper. inventory.ts re-exports ApiError for its
 * existing consumers.
 */
import { apiBaseUrl } from "./api";
import { readCsrfToken } from "./auth";

export class ApiError extends Error {
  code: string;
  status: number;
  detail: unknown;

  constructor(code: string, message: string, status: number, detail: unknown) {
    super(message);
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

export interface ListEnvelope<ItemT> {
  items: ItemT[];
  total: number;
  limit: number;
  offset: number;
}

async function parseError(response: Response): Promise<never> {
  let code = "internal_error";
  let message = `request failed: ${response.status}`;
  let detail: unknown = null;
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string; detail?: unknown };
    };
    if (body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      detail = body.error.detail ?? null;
    }
  } catch {
    // keep the status-based message
  }
  throw new ApiError(code, message, response.status, detail);
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBaseUrl()}/api/v1${path}`, {
    credentials: "include",
    ...init,
  });
  if (!response.ok) {
    return parseError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function writeHeaders(): Record<string, string> {
  return { "Content-Type": "application/json", "X-CSRF-Token": readCsrfToken() };
}

export function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
