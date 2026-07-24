/**
 * API client (task E0.4; decision D2). The frontend couples to the backend
 * only through the OpenAPI contract, cross-origin, with the base URL supplied
 * by the environment. No dev proxy, no hardcoded URL, ever.
 */

export interface HealthResponse {
  status: string;
  version: string;
  build_sha: string;
  database: string;
}

export function apiBaseUrl(): string {
  const base = import.meta.env.VITE_API_BASE_URL;
  if (!base) {
    throw new Error(
      "VITE_API_BASE_URL is not set; the frontend cannot locate the API (decision D2)",
    );
  }
  return base.replace(/\/$/, "");
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(`${apiBaseUrl()}/api/v1${path}`, {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(`API request failed: ${response.status} ${path}`);
  }
  return (await response.json()) as T;
}

export function fetchHealth(): Promise<HealthResponse> {
  return fetchJson<HealthResponse>("/health");
}
