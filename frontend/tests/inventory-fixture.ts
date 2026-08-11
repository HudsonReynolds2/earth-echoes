/**
 * Inventory fixture (task E1.8). Mirrors the E1.9 demo fixture BY NAME
 * (docs/INTERFACES.md documents the canonical set): org "Earth Echoes Demo",
 * deployments "Redwood Coast"/"High Desert", three pods each, one aggregator
 * per pod, listeners at varied counts with deterministic locally-administered
 * MACs. Stable literal UUIDs so route tests can embed them.
 */

export const ORG = {
  id: "e0e00000-0000-4000-8000-000000000001",
  name: "Earth Echoes Demo",
  tags: [] as string[],
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

interface FixturePod {
  id: string;
  deployment_id: string;
  name: string;
  tags: string[];
  agg_uuid: string;
  agg_id: string;
  listeners: number;
  prefix: string;
  mac_block: string;
}

const RC = "d1000000-0000-4000-8000-000000000001";
const HD = "d2000000-0000-4000-8000-000000000002";

const PODS: FixturePod[] = [
  {
    id: "b1100000-0000-4000-8000-000000000001",
    deployment_id: RC,
    name: "Pod 01 · Alder Creek",
    tags: ["coastal"],
    agg_uuid: "demo-agg-rc-01",
    agg_id: "a1100000-0000-4000-8000-000000000001",
    listeners: 8,
    prefix: "alder-creek",
    mac_block: "02:EE:0E:01:01",
  },
  {
    id: "b1200000-0000-4000-8000-000000000002",
    deployment_id: RC,
    name: "Pod 02 · Ridge Line",
    tags: ["ridge"],
    agg_uuid: "demo-agg-rc-02",
    agg_id: "a1200000-0000-4000-8000-000000000002",
    listeners: 5,
    prefix: "ridge-line",
    mac_block: "02:EE:0E:01:02",
  },
  {
    id: "b1300000-0000-4000-8000-000000000003",
    deployment_id: RC,
    name: "Pod 03 · Tarn Meadow",
    tags: [],
    agg_uuid: "demo-agg-rc-03",
    agg_id: "a1300000-0000-4000-8000-000000000003",
    listeners: 3,
    prefix: "tarn-meadow",
    mac_block: "02:EE:0E:01:03",
  },
  {
    id: "b2100000-0000-4000-8000-000000000004",
    deployment_id: HD,
    name: "Pod 01 · Basin Flat",
    tags: ["solar"],
    agg_uuid: "demo-agg-hd-01",
    agg_id: "a2100000-0000-4000-8000-000000000004",
    listeners: 6,
    prefix: "basin-flat",
    mac_block: "02:EE:0E:02:01",
  },
  {
    id: "b2200000-0000-4000-8000-000000000005",
    deployment_id: HD,
    name: "Pod 02 · Mesa Rim",
    tags: [],
    agg_uuid: "demo-agg-hd-02",
    agg_id: "a2200000-0000-4000-8000-000000000005",
    listeners: 4,
    prefix: "mesa-rim",
    mac_block: "02:EE:0E:02:02",
  },
  {
    id: "b2300000-0000-4000-8000-000000000006",
    deployment_id: HD,
    name: "Pod 03 · Dry Wash",
    tags: [],
    agg_uuid: "demo-agg-hd-03",
    agg_id: "a2300000-0000-4000-8000-000000000006",
    listeners: 2,
    prefix: "dry-wash",
    mac_block: "02:EE:0E:02:03",
  },
];

const STAMP = { created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z" };

export const listeners = PODS.flatMap((pod) =>
  Array.from({ length: pod.listeners }, (_, i) => ({
    mac: `${pod.mac_block}:${String(i + 1).padStart(2, "0")}`,
    name: `${pod.prefix}-${String(i + 1).padStart(2, "0")}`,
    aggregator_id: pod.agg_id,
    deployment_id: pod.deployment_id,
    gps_lat: i % 2 === 0 ? 47.6 + i / 100 : null,
    gps_lon: i % 2 === 0 ? -121.88 - i / 100 : null,
    tags: i === 0 ? pod.tags : [],
    // E3.12/D60: real status, and most fixture devices have never spoken —
    // which is the honest default and the one the guard test leans on.
    status: i === 0 ? "sleeping" : "unknown",
    ...STAMP,
  })),
);

export const aggregators = PODS.map((pod) => ({
  id: pod.agg_id,
  pod_id: pod.id,
  aggregator_uuid: pod.agg_uuid,
  balena_uuid: null,
  name: null,
  tags: [] as string[],
  listener_count: pod.listeners,
  status: "unknown" as const,
  ...STAMP,
}));

export const pods = PODS.map((pod) => ({
  id: pod.id,
  deployment_id: pod.deployment_id,
  name: pod.name,
  tags: pod.tags,
  aggregator: aggregators.find((agg) => agg.pod_id === pod.id) ?? null,
  listener_count: pod.listeners,
  status: "unknown" as const,
  ...STAMP,
}));

export const deployments = [
  {
    id: RC,
    organization_id: ORG.id,
    name: "Redwood Coast",
    slug: "redwood-coast",
    tags: ["coastal"],
    pod_count: 3,
    listener_count: 16,
    ...STAMP,
  },
  {
    id: HD,
    organization_id: ORG.id,
    name: "High Desert",
    slug: "high-desert",
    tags: ["ridge"],
    pod_count: 3,
    listener_count: 12,
    ...STAMP,
  },
];

export const FIXTURE_IDS = {
  org: ORG.id,
  redwoodCoast: RC,
  highDesert: HD,
  alderCreekPod: PODS[0].id,
  alderCreekAgg: PODS[0].agg_id,
  firstListenerMac: "02:EE:0E:01:01:01",
};

/**
 * The D7 wire grammar over an in-memory list, so component tests exercise the
 * real query-string contract (sort/limit/offset/name/tag/parent filters).
 */
export function applyListParams<T extends Record<string, unknown>>(
  items: T[],
  url: URL,
): { items: T[]; total: number; limit: number; offset: number } {
  let rows = [...items];
  for (const parent of ["organization_id", "deployment_id", "pod_id", "aggregator_id"] as const) {
    const wanted = url.searchParams.get(parent);
    if (wanted) {
      rows = rows.filter((row) => row[parent] === wanted);
    }
  }
  const name = url.searchParams.get("name");
  if (name) {
    rows = rows.filter((row) =>
      String(row.name ?? "")
        .toLowerCase()
        .includes(name.toLowerCase()),
    );
  }
  const tag = url.searchParams.get("tag");
  if (tag) {
    rows = rows.filter((row) => Array.isArray(row.tags) && row.tags.includes(tag));
  }
  const sort = url.searchParams.get("sort") ?? "name";
  const descending = sort.startsWith("-");
  const field = descending ? sort.slice(1) : sort;
  rows.sort((a, b) => {
    const left = String(a[field] ?? "");
    const right = String(b[field] ?? "");
    return descending ? right.localeCompare(left) : left.localeCompare(right);
  });
  const limit = Number(url.searchParams.get("limit") ?? 50);
  const offset = Number(url.searchParams.get("offset") ?? 0);
  return { items: rows.slice(offset, offset + limit), total: rows.length, limit, offset };
}
