/**
 * An in-memory stand-in for the provisioning API (task E4.10), built from the
 * inventory fixture so the board names real fixture Pods and Listeners.
 *
 * `provisioningHandlers()` is installed per test with `server.use`, the
 * users-admin and services pattern. The global server only answers the list
 * with nothing, so every other page that never provisions stays unaffected.
 */
import { http, HttpResponse } from "msw";

import { aggregators, listeners, pods } from "./inventory-fixture";

const API = "http://api.test/api/v1";

type Status = "generated" | "flashed" | "registered" | "confirmed" | "mismatch";

interface FixtureRecord {
  id: string;
  target_type: "aggregator" | "listener";
  target_id: string;
  pod_id: string;
  aggregator_uuid: string;
  file_kind: string;
  file_path: string;
  config_revision_id: string | null;
  config_checksum: string | null;
  status: Status;
  status_history: { status: Status; at: string; actor: string | null }[];
}

interface FixtureBundle {
  id: string;
  deployment_id: string;
  scope_type: "pod" | "deployment";
  scope_id: string;
  plan: Record<string, { mode: string; listener_macs: string[] }>;
  template_version: number;
  firmware_key_version: number;
  archive_checksum: string;
  generated_by: string | null;
  generated_at: string;
  record_count: number;
  records: FixtureRecord[];
}

export const bundleStore = new Map<string, FixtureBundle>();
export const createdBodies: unknown[] = [];
let counter = 0;

const AT = "2026-11-13T12:00:00Z";

function recordsFor(podId: string, mode: string, named: string[]): FixtureRecord[] {
  const pod = pods.find((p) => p.id === podId);
  if (!pod?.aggregator) return [];
  const agg = aggregators.find((a) => a.pod_id === podId);
  const podListeners = listeners.filter((l) => l.aggregator_id === agg?.id);
  const folder = `pods/${podId}`;
  const listenerRecords = podListeners.map((listener) => {
    const own = mode === "per_listener" || named.includes(listener.mac);
    return {
      id: `r-${++counter}`,
      target_type: "listener" as const,
      target_id: listener.mac,
      pod_id: podId,
      aggregator_uuid: agg?.aggregator_uuid ?? "",
      file_kind: own ? "listener_file" : "pod_file",
      file_path: own
        ? `${folder}/listeners/${listener.mac.replaceAll(":", "-")}.yaml`
        : `${folder}/device_config.yaml`,
      config_revision_id: null,
      config_checksum: null,
      status: "generated" as Status,
      status_history: [{ status: "generated" as Status, at: AT, actor: null }],
    };
  });
  return [
    ...listenerRecords,
    {
      id: `r-${++counter}`,
      target_type: "aggregator",
      target_id: agg?.id ?? "",
      pod_id: podId,
      aggregator_uuid: agg?.aggregator_uuid ?? "",
      file_kind: "settings_yaml",
      file_path: `${folder}/aggregator/settings.yaml`,
      config_revision_id: null,
      config_checksum: null,
      status: "generated",
      status_history: [{ status: "generated", at: AT, actor: null }],
    },
  ];
}

/** Put one bundle in the store, as if generated earlier. */
export function seedBundle(podId: string, mode = "per_pod", named: string[] = []): FixtureBundle {
  const pod = pods.find((p) => p.id === podId);
  const records = recordsFor(podId, mode, named);
  const bundle: FixtureBundle = {
    id: `bundle-${++counter}-0000-4000-8000-000000000000`,
    deployment_id: pod?.deployment_id ?? "",
    scope_type: "pod",
    scope_id: podId,
    plan: { [podId]: { mode, listener_macs: named } },
    template_version: 1,
    firmware_key_version: 1,
    archive_checksum: "sha256:" + "0".repeat(64),
    generated_by: null,
    generated_at: AT,
    record_count: records.length,
    records,
  };
  bundleStore.set(bundle.id, bundle);
  return bundle;
}

/** Move one record, as the reconciler would. */
export function setStatus(bundleId: string, targetId: string, status: Status): void {
  const record = bundleStore.get(bundleId)?.records.find((r) => r.target_id === targetId);
  if (!record) throw new Error(`no record ${targetId} in ${bundleId}`);
  record.status = status;
  record.status_history.push({ status, at: AT, actor: null });
}

export function resetProvisioning(): void {
  bundleStore.clear();
  createdBodies.length = 0;
}

/** The list item: the bundle without its records (`BundleOut`). */
const summary = (bundle: FixtureBundle) =>
  Object.fromEntries(Object.entries(bundle).filter(([key]) => key !== "records"));

export function provisioningHandlers() {
  return [
    http.get(`${API}/provisioning/bundles`, ({ request }) => {
      const deployment = new URL(request.url).searchParams.get("deployment_id");
      const items = [...bundleStore.values()]
        .filter((b) => !deployment || b.deployment_id === deployment)
        .reverse()
        .map(summary);
      return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 });
    }),
    http.get(`${API}/provisioning/bundles/:id`, ({ params }) => {
      const bundle = bundleStore.get(String(params.id));
      return bundle
        ? HttpResponse.json(bundle)
        : HttpResponse.json(
            { error: { code: "not_found", message: "bundle not found", detail: null } },
            { status: 404 },
          );
    }),
    http.post(`${API}/provisioning/bundles`, async ({ request }) => {
      const body = (await request.json()) as {
        deployment_id: string;
        scope: "pod" | "deployment";
        pods: { pod_id: string; mode: string; listener_macs: string[] }[];
      };
      createdBodies.push(body);
      const records = body.pods.flatMap((p) => recordsFor(p.pod_id, p.mode, p.listener_macs));
      const bundle: FixtureBundle = {
        id: `bundle-${++counter}-1111-4000-8000-000000000000`,
        deployment_id: body.deployment_id,
        scope_type: body.scope,
        scope_id: body.scope === "pod" ? body.pods[0].pod_id : body.deployment_id,
        plan: Object.fromEntries(
          body.pods.map((p) => [p.pod_id, { mode: p.mode, listener_macs: p.listener_macs }]),
        ),
        template_version: 1,
        firmware_key_version: 1,
        archive_checksum: "sha256:" + "1".repeat(64),
        generated_by: null,
        generated_at: AT,
        record_count: records.length,
        records,
      };
      bundleStore.set(bundle.id, bundle);
      return HttpResponse.json(bundle, { status: 201 });
    }),
    http.patch(`${API}/provisioning/records/:id`, async ({ params, request }) => {
      const { status: to } = (await request.json()) as { status: "flashed" | "generated" };
      const from = to === "flashed" ? "generated" : "flashed";
      for (const bundle of bundleStore.values()) {
        const record = bundle.records.find((r) => r.id === String(params.id));
        if (record) {
          if (record.status === from) {
            record.status = to;
            record.status_history.push({ status: to, at: AT, actor: "me" });
          }
          return HttpResponse.json(record);
        }
      }
      return HttpResponse.json(
        { error: { code: "not_found", message: "record not found", detail: null } },
        { status: 404 },
      );
    }),
  ];
}
