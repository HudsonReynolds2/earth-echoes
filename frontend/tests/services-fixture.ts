/**
 * Deployment services fixture (task E5.12a). Mirrors the real API's shapes:
 * all five services always present, configured or not, in spec 16.2's order;
 * set secrets rendered as the D51 keep sentinel and NEVER as a value — which
 * is what lets the component test assert that no input is ever populated from
 * a response.
 */
import { FIXTURE_IDS } from "./inventory-fixture";

export const SECRET_SENTINEL = { $secret_set: true };

export interface FixtureService {
  service_key: string;
  configured: boolean;
  status: "untested" | "verified" | "failed";
  status_reason: string | null;
  last_tested_at: string | null;
  consecutive_failures: number;
  settings: Record<string, unknown>;
}

/** deployment id -> the five rows. */
export const serviceStore = new Map<string, Record<string, FixtureService>>();

const blank = (key: string): FixtureService => ({
  service_key: key,
  configured: false,
  status: "untested",
  status_reason: null,
  last_tested_at: null,
  consecutive_failures: 0,
  settings: {},
});

export function seedServices() {
  serviceStore.clear();
  serviceStore.set(FIXTURE_IDS.redwoodCoast, {
    mqtt: {
      service_key: "mqtt",
      configured: true,
      status: "verified",
      status_reason: null,
      last_tested_at: "2026-08-13T14:22:08Z",
      consecutive_failures: 0,
      settings: {
        host: "kvm-01.example.org",
        port: 8883,
        tls_enabled: true,
        ca_cert_pem: null,
        username: "platform",
        password: SECRET_SENTINEL,
      },
    },
    influx: {
      service_key: "influx",
      configured: true,
      status: "failed",
      status_reason: "Write rejected: the token is not authorized for this database.",
      last_tested_at: "2026-08-13T14:22:09Z",
      consecutive_failures: 1,
      settings: {
        url: "https://influx.example.org:8181",
        database: "recordings",
        token: SECRET_SENTINEL,
      },
    },
    prometheus: blank("prometheus"),
    grafana: blank("grafana"),
    s3: blank("s3"),
  });
  serviceStore.set(FIXTURE_IDS.highDesert, {
    mqtt: blank("mqtt"),
    influx: blank("influx"),
    prometheus: blank("prometheus"),
    grafana: blank("grafana"),
    s3: blank("s3"),
  });
}
seedServices();

export function fixtureServices(deploymentId: string) {
  const rows = serviceStore.get(deploymentId);
  return {
    deployment_id: deploymentId,
    services: rows ?? {
      mqtt: blank("mqtt"),
      influx: blank("influx"),
      prometheus: blank("prometheus"),
      grafana: blank("grafana"),
      s3: blank("s3"),
    },
  };
}

/**
 * The rollup, computed the way `status.py::roll_up` computes it, so the
 * fixture cannot tell the UI a story the backend would not. Object storage is
 * required only when it is configured (D123's reading: absent credentials
 * mean the deployment does not upload raw audio).
 */
export function fixtureServicesStatus(deploymentId: string) {
  const rows = fixtureServices(deploymentId).services;
  const required = (key: string) => key !== "s3" || rows.s3.configured;
  const requiredKeys = Object.keys(rows).filter(required);
  const configured = requiredKeys.filter((key) => rows[key].configured);

  let rollup = "pending_verification";
  if (configured.length === 0) {
    rollup = "unconfigured";
  } else if (requiredKeys.some((key) => rows[key].status === "failed")) {
    rollup = "degraded";
  } else if (requiredKeys.every((key) => rows[key].configured && rows[key].status === "verified")) {
    rollup = "verified";
  }

  return {
    deployment_id: deploymentId,
    services_status: rollup,
    degrade_after_failures: 2,
    services: Object.fromEntries(
      Object.entries(rows).map(([key, row]) => [
        key,
        {
          service_key: key,
          configured: row.configured,
          required: required(key),
          status: row.status,
          status_reason: row.status_reason,
          last_tested_at: row.last_tested_at,
          consecutive_failures: row.consecutive_failures,
        },
      ]),
    ),
  };
}
