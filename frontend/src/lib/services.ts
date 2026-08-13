/**
 * Deployment services client (task E5.12a; spec 16.2, 16.3, 16.5).
 *
 * Shaped after `lib/inventory.ts` — the typed `ApiError` from `lib/http.ts`,
 * one exported function per call, flat query keys — so a reader who knows the
 * inventory client knows this one.
 *
 * **The field table is the other half of this module and is the point.** The
 * five services have five different field sets, and the wizard renders them
 * from `SERVICE_SCHEMA` rather than from five hand-written forms. The
 * canonical schema is `backend/app/services/schemas.py`: one Pydantic model
 * per service, `extra="forbid"`, which is what actually decides whether a
 * credential is stored. This table is a mirror of it and
 * `tests/services-schema.test.ts` fails if the two diverge — the same
 * cross-language parity discipline `lib/rbac.ts` is held to.
 *
 * **Secrets are write-only here as well as in the API.** A stored credential
 * comes back as the D51 keep sentinel `{"$secret_set": true}` and never as a
 * value, so no input on this page is ever populated from a response; a form
 * the operator never held the secrets for round-trips through PUT unchanged.
 */
import { request, writeHeaders } from "./http";

export { ApiError } from "./http";

/** The spec 16.2 table's five, in its order. */
export const SERVICE_KEYS = ["mqtt", "influx", "prometheus", "grafana", "s3"] as const;
export type ServiceKey = (typeof SERVICE_KEYS)[number];

/**
 * Per-connection status (`deployment_service.status`). **Not the rollup and
 * not device status** — three vocabularies that must not be rendered through
 * one another (D40, and the DES three-channel rule).
 */
export type ServiceStatusValue = "untested" | "verified" | "failed";

/** The deployment rollup (`deployment.services_status`, spec 16.5). */
export type ServicesStatusValue = "unconfigured" | "pending_verification" | "verified" | "degraded";

/** A tester's verdict. `not_required` and `not_configured` are NOT failures. */
export type TesterOutcome = "pass" | "fail" | "not_required" | "not_configured";

// --- the schema the forms render from ----------------------------------------

/**
 * How one field is entered. `secret` is a behavior and not a widget: it is
 * write-only, renders set-ness rather than a value, and submits the keep
 * sentinel when the operator does not replace it.
 */
export type ServiceFieldType = "text" | "textarea" | "number" | "boolean" | "secret";

export interface ServiceField {
  /** Wire name. Identical to the Pydantic model's field name. */
  name: string;
  label: string;
  type: ServiceFieldType;
  /** Required by the model — no default on the Python side. */
  required: boolean;
  help?: string;
  placeholder?: string;
}

export interface ServiceDescriptor {
  key: ServiceKey;
  /** What the operator calls it, not what the database calls it. */
  label: string;
  /** One line under the heading: what this service is for in this platform. */
  blurb: string;
  fields: ServiceField[];
}

/**
 * The five, in spec 16.2's order, with each model's fields in the model's
 * order. Field names, secret-ness and required-ness are all asserted against
 * `schemas.py` by the parity test; the labels and help text are this file's
 * own and are the operator-facing half.
 */
export const SERVICE_SCHEMA: ServiceDescriptor[] = [
  {
    key: "mqtt",
    label: "Mosquitto",
    blurb: "The control plane. Every device's configuration and status rides this broker.",
    fields: [
      {
        name: "host",
        label: "Host",
        type: "text",
        required: true,
        placeholder: "broker.example.org",
      },
      { name: "port", label: "Port", type: "number", required: true, placeholder: "8883" },
      { name: "tls_enabled", label: "TLS enabled", type: "boolean", required: false },
      {
        name: "ca_cert_pem",
        label: "CA certificate (PEM)",
        type: "textarea",
        required: false,
        help: "The trust anchor the platform verifies the broker against. Public material, not a credential — it is stored on the row rather than in the secret store.",
      },
      { name: "username", label: "Username", type: "text", required: true },
      {
        name: "password",
        label: "Password",
        type: "secret",
        required: true,
        help: "Required: the broker row cannot exist without it.",
      },
    ],
  },
  {
    key: "influx",
    label: "InfluxDB 3",
    blurb: "Where detections and telemetry land.",
    fields: [
      {
        name: "url",
        label: "URL",
        type: "text",
        required: true,
        placeholder: "https://influx.example.org:8181",
      },
      { name: "database", label: "Database", type: "text", required: true },
      { name: "token", label: "Token", type: "secret", required: false },
    ],
  },
  {
    key: "prometheus",
    label: "Prometheus",
    blurb: "Device metrics. Two endpoints with two roles, so both are asked for.",
    fields: [
      {
        name: "read_url",
        label: "Read URL",
        type: "text",
        required: true,
        help: "What the platform queries.",
        placeholder: "https://prometheus.example.org:9090",
      },
      {
        name: "remote_write_url",
        label: "Remote-write URL",
        type: "text",
        required: true,
        help: "What the Aggregators' agents push to. Prometheus does not enable its remote-write receiver by default; the test says so if it is off.",
      },
      { name: "remote_write_user", label: "Remote-write user", type: "text", required: true },
      {
        name: "remote_write_password",
        label: "Remote-write password",
        type: "secret",
        required: false,
      },
    ],
  },
  {
    key: "grafana",
    label: "Grafana",
    blurb: "Dashboards and alert delivery.",
    fields: [
      {
        name: "base_url",
        label: "Base URL",
        type: "text",
        required: true,
        placeholder: "https://grafana.example.org:3000",
      },
      {
        name: "service_account_token",
        label: "Service account token",
        type: "secret",
        required: false,
        help: "Supply this, or an admin account below and the platform mints one.",
      },
      { name: "admin_username", label: "Admin username", type: "text", required: false },
      {
        name: "admin_password",
        label: "Admin password",
        type: "secret",
        required: false,
        help: "Used once, to have Grafana issue a service account token. Never sent again after that.",
      },
    ],
  },
  {
    key: "s3",
    label: "Object storage",
    blurb: "Raw-audio upload. Leave every field blank if this deployment does not upload audio.",
    fields: [
      { name: "bucket", label: "Bucket", type: "text", required: true },
      { name: "region", label: "Region", type: "text", required: false },
      {
        name: "endpoint",
        label: "Endpoint",
        type: "text",
        required: false,
        help: "MinIO and other S3-compatible stores need one. Real AWS does not.",
      },
      { name: "access_key", label: "Access key", type: "secret", required: false },
      { name: "secret_key", label: "Secret key", type: "secret", required: false },
    ],
  },
];

export const DESCRIPTOR_BY_KEY: Record<ServiceKey, ServiceDescriptor> = Object.fromEntries(
  SERVICE_SCHEMA.map((descriptor) => [descriptor.key, descriptor]),
) as Record<ServiceKey, ServiceDescriptor>;

// --- wire types ----------------------------------------------------------------

/**
 * The D51 keep sentinel. Reused from the config editor rather than reinvented
 * — one wire shape for "keep what is stored" across the whole frontend.
 */
export const KEEP_SENTINEL = { $secret_set: true } as const;

export function isSecretSet(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    "$secret_set" in value &&
    (value as { $secret_set: unknown }).$secret_set === true
  );
}

/** One service's redacted settings: values for plain fields, the sentinel for
 * a set secret, absent for an unset one. `unknown` per value on purpose —
 * the shape is per-service and the descriptor is what reads it. */
export type ServiceSettingsMap = Record<string, unknown>;

export interface Service {
  service_key: string;
  configured: boolean;
  status: ServiceStatusValue;
  status_reason: string | null;
  last_tested_at: string | null;
  consecutive_failures: number;
  settings: ServiceSettingsMap;
}

export interface Services {
  deployment_id: string;
  services: Record<string, Service>;
}

export interface ServiceStatus {
  service_key: string;
  configured: boolean;
  /** Whether this service must verify for the deployment to. Object storage
   * is conditionally required (spec 16.2), so this is never hardcoded. */
  required: boolean;
  status: ServiceStatusValue;
  status_reason: string | null;
  last_tested_at: string | null;
  consecutive_failures: number;
}

export interface ServicesStatus {
  deployment_id: string;
  services_status: ServicesStatusValue;
  /** The platform's demotion threshold, so the UI can say "1 of 2 failed
   * checks" without knowing the number itself. */
  degrade_after_failures: number;
  services: Record<string, ServiceStatus>;
}

export interface Check {
  name: string;
  passed: boolean;
  detail: string;
  /** Non-empty on every failing check — S5's premise is that an operator
   * reads a failure and fixes their service. */
  remedy: string;
  elapsed_ms: number;
}

export interface TestResult {
  service_key: string;
  outcome: TesterOutcome;
  checks: Check[];
}

export interface ServicesTest {
  deployment_id: string;
  services_status: ServicesStatusValue;
  results: TestResult[];
}

/** A submitted service: field name -> plaintext, keep sentinel, or null. */
export type ServiceSettingsIn = Record<string, unknown>;
export type ServicesIn = Partial<Record<ServiceKey, ServiceSettingsIn>>;

// --- calls -----------------------------------------------------------------------

export function getServices(deploymentId: string) {
  return request<Services>(`/deployments/${deploymentId}/services`);
}

export function getServicesStatus(deploymentId: string) {
  return request<ServicesStatus>(`/deployments/${deploymentId}/services/status`);
}

/**
 * Save any subset of the five. A service present in the body is written
 * WHOLESALE — every field omitted from it is cleared — which is why the form
 * submits all of its own fields and never a patch of the ones that changed.
 * A service absent from the body is left completely alone.
 */
export function putServices(deploymentId: string, services: ServicesIn) {
  return request<Services>(`/deployments/${deploymentId}/services`, {
    method: "PUT",
    headers: writeHeaders(),
    body: JSON.stringify({ services }),
  });
}

/**
 * Run the connection tests. With no `services`, tests what is STORED and the
 * verdicts are recorded; with candidate credentials, the results come back
 * but nothing is written — spec 16.2's "validates each entry before accepting
 * it" is precisely a test that has not been accepted yet.
 */
export function testServices(deploymentId: string, services: ServicesIn = {}) {
  return request<ServicesTest>(`/deployments/${deploymentId}/services/test`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({ services }),
  });
}

/** Flat query keys, the inventory client's convention. */
export const servicesKey = (deploymentId: string) => ["services", deploymentId];
export const servicesStatusKey = (deploymentId: string) => ["services-status", deploymentId];
