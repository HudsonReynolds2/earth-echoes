/**
 * Config fixture (task E2.7). A catalog spanning every editor type PLUS
 * `test.demo_knob` — a key no component references by name; its row growing
 * a working editor purely from this data IS the E2.7 acceptance (spec 5.3,
 * "the catalog is data"). The in-memory override store and the ~40-line
 * merge below back the effective/overrides/preview handlers, so no-op
 * detection and provenance are REAL in tests, not canned. Secret discipline
 * holds here too: reads always emit the sentinel, never plaintext.
 */
import { aggregators, deployments, FIXTURE_IDS, listeners, ORG, pods } from "./inventory-fixture";

export interface FixtureCatalogKey {
  key: string;
  value_type: string;
  enum_values: unknown[] | null;
  min_value: number | null;
  max_value: number | null;
  default: unknown;
  lowest_level: string;
  secret: boolean;
  resolution: "override" | "inventory";
  write_restricted: string | null;
  notes: string;
}

function key(partial: Partial<FixtureCatalogKey> & { key: string }): FixtureCatalogKey {
  return {
    value_type: "string",
    enum_values: null,
    min_value: null,
    max_value: null,
    default: null,
    lowest_level: "listener",
    secret: false,
    resolution: "override",
    write_restricted: null,
    notes: "",
    ...partial,
  };
}

export const CATALOG_FIXTURE = {
  version: 4,
  items: [
    key({
      key: "audio.sample_rate_hz",
      value_type: "int",
      enum_values: [8000, 16000, 48000, 96000, 192000],
      default: 48000,
    }),
    key({ key: "audio.bits_per_sample", value_type: "int", enum_values: [16, 24], default: 16 }),
    key({
      key: "capture.mode",
      value_type: "string",
      enum_values: ["continuous", "duty_cycle", "schedule"],
      default: "duty_cycle",
    }),
    key({ key: "capture.duty_on_seconds", value_type: "int", default: 60 }),
    key({ key: "capture.schedule", value_type: "object", default: null }),
    key({
      key: "analysis.confidence_threshold",
      value_type: "float",
      min_value: 0,
      max_value: 1,
      default: 0.5,
      lowest_level: "aggregator",
    }),
    key({ key: "buffering.sd_enabled", value_type: "bool", default: true }),
    key({
      key: "logging.verbosity",
      value_type: "string",
      enum_values: ["error", "warn", "info", "debug", "trace"],
      default: "info",
      lowest_level: "any",
    }),
    key({ key: "network.wifi_ssid", lowest_level: "pod" }),
    key({ key: "network.wifi_password", lowest_level: "pod", secret: true }),
    key({ key: "network.aggregator_ip", lowest_level: "pod" }),
    key({ key: "upload.s3_prefix", lowest_level: "aggregator", default: "" }),
    key({
      key: "telemetry.influx_url",
      lowest_level: "deployment",
      write_restricted: "service_onboarding",
    }),
    key({ key: "identity.name", resolution: "inventory" }),
    key({ key: "identity.mac", resolution: "inventory" }),
    key({ key: "location.gps_lat", value_type: "float", resolution: "inventory" }),
    key({ key: "location.gps_lon", value_type: "float", resolution: "inventory" }),
    // THE TEST KEY: nothing in src/ names it; its working editor proves
    // catalog-drivenness (the E2.7 acceptance).
    key({
      key: "test.demo_knob",
      value_type: "string",
      enum_values: ["alpha", "beta", "gamma"],
      default: "alpha",
    }),
  ],
};

export const SECRET_SENTINEL = { $secret_set: true };

/** entity-path/id -> sparse override map (redacted form: secrets are the
 * sentinel — exactly what the real API emits). */
export const overrideStore = new Map<string, Record<string, unknown>>();

export function seedOverrides() {
  overrideStore.clear();
  overrideStore.set(`organizations/${ORG.id}`, { "capture.mode": "continuous" });
  overrideStore.set(`deployments/${FIXTURE_IDS.redwoodCoast}`, { "audio.sample_rate_hz": 96000 });
  overrideStore.set(`pods/${FIXTURE_IDS.alderCreekPod}`, {
    "network.wifi_ssid": "alder-mesh",
    "network.wifi_password": SECRET_SENTINEL,
    "capture.duty_on_seconds": 90,
  });
  overrideStore.set(`listeners/${FIXTURE_IDS.firstListenerMac}`, {
    "audio.sample_rate_hz": 192000,
  });
}
seedOverrides();

const LEVEL_OF: Record<string, string> = {
  organizations: "organization",
  deployments: "deployment",
  pods: "pod",
  aggregators: "aggregator",
  listeners: "listener",
};
const DEPTH: Record<string, number> = {
  organization: 0,
  deployment: 1,
  pod: 2,
  aggregator: 3,
  listener: 4,
};

function chainFor(entity: string, id: string): Array<{ entity: string; id: string }> {
  if (entity === "organizations") {
    return [{ entity: "organizations", id }];
  }
  if (entity === "deployments") {
    return [
      { entity: "organizations", id: ORG.id },
      { entity: "deployments", id },
    ];
  }
  if (entity === "pods") {
    const pod = pods.find((row) => row.id === id);
    return [...chainFor("deployments", pod?.deployment_id ?? ""), { entity: "pods", id }];
  }
  if (entity === "aggregators") {
    const aggregator = aggregators.find((row) => row.id === id);
    return [...chainFor("pods", aggregator?.pod_id ?? ""), { entity: "aggregators", id }];
  }
  const listener = listeners.find((row) => row.mac === id);
  const aggregator = aggregators.find((row) => row.id === listener?.aggregator_id);
  return [...chainFor("aggregators", aggregator?.id ?? ""), { entity: "listeners", id }];
}

/** The fixture merge: deepest setter wins over the catalog default;
 * inventory keys resolve from the listener row at listener level and are
 * omitted elsewhere — the D53 semantics in miniature. */
export function fixtureEffective(entity: string, id: string) {
  const level = LEVEL_OF[entity];
  const chain = chainFor(entity, id);
  const config: Record<
    string,
    { value: unknown; source: string; source_entity_id: string | null }
  > = {};
  for (const def of CATALOG_FIXTURE.items) {
    if (def.resolution === "inventory") {
      if (level !== "listener") {
        continue;
      }
      const listener = listeners.find((row) => row.mac === id);
      const value =
        def.key === "identity.name"
          ? (listener?.name ?? null)
          : def.key === "identity.mac"
            ? (listener?.mac ?? null)
            : def.key === "location.gps_lat"
              ? (listener?.gps_lat ?? null)
              : (listener?.gps_lon ?? null);
      config[def.key] = { value, source: "inventory", source_entity_id: id };
      continue;
    }
    let winner: { value: unknown; source: string; source_entity_id: string | null } = {
      value: def.default,
      source: "default",
      source_entity_id: null,
    };
    for (const link of chain) {
      const map = overrideStore.get(`${link.entity}/${link.id}`);
      if (map && def.key in map) {
        winner = { value: map[def.key], source: LEVEL_OF[link.entity], source_entity_id: link.id };
      }
    }
    config[def.key] = winner;
  }
  return {
    entity_type: level,
    entity_id: id,
    catalog_version: CATALOG_FIXTURE.version,
    config,
  };
}

export function fixtureOverrides(entity: string, id: string) {
  return {
    entity_type: LEVEL_OF[entity],
    entity_id: id,
    catalog_version: CATALOG_FIXTURE.version,
    overrides: overrideStore.get(`${entity}/${id}`) ?? {},
  };
}

export const CONFIG_ENTITY_DEPTH = DEPTH;
export const configDeployments = deployments;
