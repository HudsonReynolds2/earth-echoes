/**
 * Provisioning client (task E4.10; spec 8.5, 13; INTERFACES "Owned by E4").
 *
 * Shaped after `lib/inventory.ts` and `lib/services.ts`: the typed `ApiError`
 * from `lib/http.ts`, one exported function per call, flat query keys.
 *
 * **Record status is not device status.** The five spec 8.5 values below are a
 * pipeline a card walks through: written, came online, compared. They never
 * render through `StatusChip`'s six device states (D40, the DES three-channel
 * rule). `RecordChip` is their component.
 */
import { apiBaseUrl } from "./api";
import { ApiError, ListEnvelope, request, writeHeaders } from "./http";

export { ApiError } from "./http";

/** Spec 8.5, verbatim and in its order: the board's columns. */
export const RECORD_STATUSES = [
  "generated",
  "flashed",
  "registered",
  "confirmed",
  "mismatch",
] as const;
export type RecordStatus = (typeof RECORD_STATUSES)[number];

/** Spec 8.2's two modes and the per-Pod mix. */
export const BUNDLE_MODES = ["per_pod", "per_listener", "mixed"] as const;
export type BundleMode = (typeof BUNDLE_MODES)[number];

export type FileKind = "pod_file" | "listener_file" | "settings_yaml";

export interface RecordHistoryEntry {
  status: RecordStatus;
  at: string;
  /** A user id, or null when the system (the reconciler) moved it. */
  actor: string | null;
}

export interface ProvisioningRecord {
  id: string;
  target_type: "aggregator" | "listener";
  /** The Listener's MAC, or the Aggregator's platform row id (D75). */
  target_id: string;
  pod_id: string;
  aggregator_uuid: string;
  file_kind: FileKind;
  file_path: string;
  config_revision_id: string | null;
  config_checksum: string | null;
  status: RecordStatus;
  status_history: RecordHistoryEntry[];
}

export interface PodPlan {
  mode: BundleMode;
  listener_macs: string[];
}

export interface Bundle {
  id: string;
  deployment_id: string;
  scope_type: "pod" | "deployment";
  scope_id: string;
  plan: Record<string, PodPlan>;
  template_version: number;
  firmware_key_version: number;
  archive_checksum: string;
  generated_by: string | null;
  generated_at: string;
  record_count: number;
}

export interface BundleDetail extends Bundle {
  records: ProvisioningRecord[];
}

export interface PodChoiceIn {
  pod_id: string;
  mode: BundleMode;
  listener_macs: string[];
}

export interface CreateBundleIn {
  deployment_id: string;
  scope: "pod" | "deployment";
  pods: PodChoiceIn[];
}

/**
 * The two refusals worth explaining to an operator, from the 422 `detail`:
 * `missing` names unset network keys (configure the Pod), and `deployment`
 * names a deployment whose services are not verified (spec 16.5).
 */
export interface GenerationRefusal {
  missing?: string[];
  deployment?: string;
}

// --- calls -----------------------------------------------------------------------

export function listBundles(deploymentId: string) {
  const query = new URLSearchParams({ deployment_id: deploymentId, limit: "100" });
  return request<ListEnvelope<Bundle>>(`/provisioning/bundles?${query.toString()}`);
}

export function getBundle(bundleId: string) {
  return request<BundleDetail>(`/provisioning/bundles/${bundleId}`);
}

export function createBundle(input: CreateBundleIn) {
  return request<BundleDetail>(`/provisioning/bundles`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify(input),
  });
}

/** The statuses a person may set (spec 8.5): "flashed" marks a card written,
 * "generated" unmarks a mis-click (D187). The rest are the reconciler's. */
export type OperatorStatus = "flashed" | "generated";

export function setRecordStatus(recordId: string, status: OperatorStatus) {
  return request<ProvisioningRecord>(`/provisioning/records/${recordId}`, {
    method: "PATCH",
    headers: writeHeaders(),
    body: JSON.stringify({ status }),
  });
}

/**
 * Fetch the archive as bytes, for the same reasons `downloadStack` does: an
 * `<a href>` cannot carry a typed auth failure across origins. **The archive
 * holds each Aggregator's broker login in plaintext, by design (spec 16.4)**,
 * which is why only a role that may generate bundles may download one.
 */
export async function downloadBundle(bundleId: string): Promise<{ blob: Blob; filename: string }> {
  const response = await fetch(`${apiBaseUrl()}/api/v1/provisioning/bundles/${bundleId}/download`, {
    credentials: "include",
  });
  if (!response.ok) {
    let code = "internal_error";
    let message = `download failed: ${response.status}`;
    try {
      const body = (await response.json()) as { error?: { code?: string; message?: string } };
      code = body.error?.code ?? code;
      message = body.error?.message ?? message;
    } catch {
      // keep the status-based message
    }
    throw new ApiError(code, message, response.status, null);
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const named = /filename="([^"]+)"/.exec(disposition);
  return {
    blob: await response.blob(),
    filename: named ? named[1] : `echoes-provisioning-${bundleId}.zip`,
  };
}

/** Flat query keys, the inventory client's convention. */
export const bundlesKey = (deploymentId: string) => ["provisioning-bundles", deploymentId];
export const bundleKey = (bundleId: string) => ["provisioning-bundle", bundleId];

/** How often the board re-reads a bundle so it "fills in by itself". The
 * worker's reconciler runs every minute; 15s keeps the board within a beat of
 * it without hammering the API. */
export const BOARD_REFRESH_MS = 15_000;
