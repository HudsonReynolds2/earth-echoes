/**
 * Hierarchy/inventory client (task E1.8). Follows the users.ts template —
 * envelope-aware error parsing, CSRF header on writes — plus a typed ApiError
 * the UI can branch on (the 409 name-conflict dialog reads error.detail).
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

export interface ListParams {
  limit?: number;
  offset?: number;
  sort?: string;
  name?: string;
  tag?: string;
}

export interface Organization {
  id: string;
  name: string;
  tags: string[];
  created_at: string;
  updated_at: string;
}

export interface Deployment {
  id: string;
  organization_id: string;
  name: string;
  slug: string;
  tags: string[];
  pod_count: number;
  listener_count: number;
  created_at: string;
  updated_at: string;
}

export interface Aggregator {
  id: string;
  pod_id: string;
  aggregator_uuid: string;
  balena_uuid: string | null;
  name: string | null;
  tags: string[];
  listener_count: number;
  created_at: string;
  updated_at: string;
}

export interface Pod {
  id: string;
  deployment_id: string;
  name: string;
  tags: string[];
  aggregator: Aggregator | null;
  listener_count: number;
  created_at: string;
  updated_at: string;
}

export interface Listener {
  mac: string;
  name: string;
  aggregator_id: string;
  deployment_id: string;
  gps_lat: number | null;
  gps_lon: number | null;
  tags: string[];
  created_at: string;
  updated_at: string;
}

export interface ImportRowResult {
  row: number;
  status: "created" | "error";
  entity_id: string | null;
  name: string | null;
  error: { code: string; message: string } | null;
}

export interface ImportReport {
  committed: boolean;
  created: number;
  failed: number;
  rows: ImportRowResult[];
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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

function writeHeaders(): Record<string, string> {
  return { "Content-Type": "application/json", "X-CSRF-Token": readCsrfToken() };
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

// --- lists and items ---------------------------------------------------------

export function listOrganizations(params: ListParams = {}) {
  return request<ListEnvelope<Organization>>(`/organizations${query({ ...params })}`);
}

export function listDeployments(params: ListParams & { organization_id?: string } = {}) {
  return request<ListEnvelope<Deployment>>(`/deployments${query({ ...params })}`);
}

export function listPods(params: ListParams & { deployment_id?: string } = {}) {
  return request<ListEnvelope<Pod>>(`/pods${query({ ...params })}`);
}

export function listAggregators(params: ListParams & { pod_id?: string } = {}) {
  return request<ListEnvelope<Aggregator>>(`/aggregators${query({ ...params })}`);
}

export function listListeners(
  params: ListParams & { aggregator_id?: string; deployment_id?: string } = {},
) {
  return request<ListEnvelope<Listener>>(`/listeners${query({ ...params })}`);
}

export function getDeployment(id: string) {
  return request<Deployment>(`/deployments/${id}`);
}

export function getPod(id: string) {
  return request<Pod>(`/pods/${id}`);
}

export function getListener(mac: string) {
  return request<Listener>(`/listeners/${encodeURIComponent(mac)}`);
}

// --- writes -------------------------------------------------------------------

export function createDeployment(input: { organization_id: string; name: string; slug?: string }) {
  return request<Deployment>(`/deployments`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify(input),
  });
}

export function createPod(input: {
  deployment_id: string;
  name: string;
  aggregator?: { aggregator_uuid?: string; balena_uuid?: string; name?: string };
}) {
  return request<Pod>(`/pods`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify(input),
  });
}

export function createListener(
  input: {
    mac: string;
    name: string;
    aggregator_id: string;
    gps_lat?: number | null;
    gps_lon?: number | null;
  },
  options: { autoSuffix?: boolean } = {},
) {
  return request<Listener>(`/listeners`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({ ...input, auto_suffix: options.autoSuffix ?? false }),
  });
}

export function patchListener(
  mac: string,
  input: { name?: string; gps_lat?: number | null; gps_lon?: number | null },
) {
  return request<Listener>(`/listeners/${encodeURIComponent(mac)}`, {
    method: "PATCH",
    headers: writeHeaders(),
    body: JSON.stringify(input),
  });
}

export function deleteListener(mac: string) {
  return request<void>(`/listeners/${encodeURIComponent(mac)}`, {
    method: "DELETE",
    headers: writeHeaders(),
  });
}

export function deletePod(id: string) {
  return request<void>(`/pods/${id}`, { method: "DELETE", headers: writeHeaders() });
}

export function deleteDeployment(id: string) {
  return request<void>(`/deployments/${id}`, { method: "DELETE", headers: writeHeaders() });
}

// --- tags ----------------------------------------------------------------------

export type TaggableEntity = "organizations" | "deployments" | "pods" | "aggregators" | "listeners";

export function getTags(entity: TaggableEntity, id: string) {
  return request<{ tags: string[] }>(`/${entity}/${encodeURIComponent(id)}/tags`);
}

export function putTags(entity: TaggableEntity, id: string, tags: string[]) {
  return request<{ tags: string[] }>(`/${entity}/${encodeURIComponent(id)}/tags`, {
    method: "PUT",
    headers: writeHeaders(),
    body: JSON.stringify({ tags }),
  });
}

// --- bulk import -----------------------------------------------------------------

export function importListeners(input: {
  format: "csv" | "json";
  content: string;
  partial: boolean;
  autoSuffix: boolean;
}) {
  const params = query({
    partial: String(input.partial),
    auto_suffix: String(input.autoSuffix),
  });
  return request<ImportReport>(`/listeners/import${params}`, {
    method: "POST",
    headers: {
      "Content-Type": input.format === "csv" ? "text/csv" : "application/json",
      "X-CSRF-Token": readCsrfToken(),
    },
    body:
      input.format === "csv" ? input.content : JSON.stringify({ rows: JSON.parse(input.content) }),
  });
}

export function importAggregators(input: {
  format: "csv" | "json";
  content: string;
  partial: boolean;
}) {
  const params = query({ partial: String(input.partial) });
  return request<ImportReport>(`/aggregators/import${params}`, {
    method: "POST",
    headers: {
      "Content-Type": input.format === "csv" ? "text/csv" : "application/json",
      "X-CSRF-Token": readCsrfToken(),
    },
    body:
      input.format === "csv" ? input.content : JSON.stringify({ rows: JSON.parse(input.content) }),
  });
}
