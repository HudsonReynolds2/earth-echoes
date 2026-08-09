/**
 * Configuration client (task E2.7; INTERFACES "Owned by E2"). One function
 * per call over lib/http.ts, plus the PURE helpers the editor leans on -
 * the level-rule truth table (editableAt), provenance resolution, unit
 * derivation, and the one-PUT draft builder - all unit-tested in
 * config-lib.test.ts so the JSX stays declarative.
 */
import { ListEnvelope, query, request, writeHeaders } from "./http";
import { TaggableEntity } from "./inventory";

export type { ListEnvelope } from "./http";

export type ConfigEntity = TaggableEntity;
export type EntityLevel = "organization" | "deployment" | "pod" | "aggregator" | "listener";

// Merge order, root first (mirrors the backend LEVELS constant).
export const LEVELS: EntityLevel[] = [
  "organization",
  "deployment",
  "pod",
  "aggregator",
  "listener",
];
export const LEVEL_DEPTH: Record<EntityLevel, number> = {
  organization: 0,
  deployment: 1,
  pod: 2,
  aggregator: 3,
  listener: 4,
};

export const ENTITY_PATHS: Record<EntityLevel, ConfigEntity> = {
  organization: "organizations",
  deployment: "deployments",
  pod: "pods",
  aggregator: "aggregators",
  listener: "listeners",
};

export interface CatalogKey {
  key: string;
  value_type: "int" | "float" | "bool" | "string" | "object" | (string & {});
  enum_values: unknown[] | null;
  min_value: number | null;
  max_value: number | null;
  default: unknown;
  lowest_level: EntityLevel | "any";
  secret: boolean;
  resolution: "override" | "inventory";
  write_restricted: string | null;
  notes: string;
}

export interface Catalog {
  version: number;
  items: CatalogKey[];
}

export type ProvenanceSource = EntityLevel | "default" | "inventory";

export interface EffectiveKey {
  value: unknown;
  source: ProvenanceSource;
  source_entity_id: string | null;
}

export interface EffectiveConfig {
  entity_type: EntityLevel;
  entity_id: string;
  catalog_version: number;
  config: Record<string, EffectiveKey>;
}

export type OverrideMap = Record<string, unknown>;

export interface Overrides {
  entity_type: EntityLevel;
  entity_id: string;
  catalog_version: number;
  overrides: OverrideMap;
}

/** The redacted form of a SET secret ({"$secret_set": true}); round-trips
 * through PUT as the keep sentinel. */
export function isSecretSet(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    "$secret_set" in value &&
    (value as { $secret_set: unknown }).$secret_set === true
  );
}

export const KEEP_SENTINEL = { $secret_set: true } as const;

// --- calls -------------------------------------------------------------------

export function getCatalog() {
  return request<Catalog>(`/config/catalog`);
}

export function getEffectiveConfig(entity: ConfigEntity, id: string) {
  return request<EffectiveConfig>(`/${entity}/${encodeURIComponent(id)}/config/effective`);
}

export function getOverrides(entity: ConfigEntity, id: string) {
  return request<Overrides>(`/${entity}/${encodeURIComponent(id)}/config/overrides`);
}

export function putOverrides(entity: ConfigEntity, id: string, overrides: OverrideMap) {
  return request<Overrides>(`/${entity}/${encodeURIComponent(id)}/config/overrides`, {
    method: "PUT",
    headers: writeHeaders(),
    body: JSON.stringify({ overrides }),
  });
}

export interface RevisionListItem {
  id: string;
  target_type: "aggregator" | "listener";
  target_id: string;
  deployment_id: string;
  schema_version: number;
  checksum: string;
  state: string;
  created_by: string | null;
  created_at: string;
}

export function listRevisions(
  entity: "aggregators" | "listeners",
  id: string,
  params: { state?: string; limit?: number; offset?: number } = {},
) {
  return request<ListEnvelope<RevisionListItem>>(
    `/${entity}/${encodeURIComponent(id)}/revisions${query({ ...params })}`,
  );
}

// --- bulk edit + selections (E2.8; the E2.6 wire contract) -------------------

export interface SelectionQueryWire {
  entity_type: EntityLevel;
  scope?: { deployment_id: string } | null;
  where?: Record<string, unknown> | null;
}

export type PlanSelection = SelectionQueryWire | { selection_id: string };

export interface PlanBody {
  selection: PlanSelection;
  changes: OverrideMap;
  level: "target" | "organization" | "deployment" | "pod" | "aggregator";
}

export interface DevicePlanItem {
  target_type: "aggregator" | "listener";
  target_id: string;
  name: string;
  pod_id: string;
  pod_name: string;
  deployment_id: string;
  changed_keys: string[];
  no_op: boolean;
  before: Record<string, EffectiveKey>;
  after: Record<string, EffectiveKey>;
}

export interface ApplyResult {
  state: string;
  publish_enabled: boolean;
  revisions: Array<{
    revision_id: string;
    target_type: string;
    target_id: string;
    deployment_id: string;
    changed_keys: string[];
    checksum: string;
  }>;
}

export function previewConfig(body: PlanBody, params: { limit?: number; offset?: number } = {}) {
  return request<ListEnvelope<DevicePlanItem>>(`/config/preview${query({ ...params })}`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify(body),
  });
}

export function applyConfig(body: PlanBody) {
  return request<ApplyResult>(`/config/apply`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify(body),
  });
}

export interface SavedSelection {
  id: string;
  name: string;
  query: SelectionQueryWire;
  created_by: string | null;
  created_at: string;
}

export function listSelections() {
  return request<ListEnvelope<SavedSelection>>(`/selections`);
}

export function createSelection(input: { name: string; query: SelectionQueryWire }) {
  return request<SavedSelection>(`/selections`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify(input),
  });
}

// --- pure helpers (the unit-tested truth tables) -----------------------------

/** D50, exact: a key is editable at a level iff that level is AT or ABOVE
 * (an ancestor of) the key's lowest level; 'any' behaves as listener, so it
 * is editable everywhere. Inventory and service-restricted keys are never
 * editable through overrides regardless of level. */
export function editableAt(def: CatalogKey, level: EntityLevel): boolean {
  if (def.resolution === "inventory" || def.write_restricted !== null) {
    return false;
  }
  const lowest = def.lowest_level === "any" ? "listener" : def.lowest_level;
  return LEVEL_DEPTH[level] <= LEVEL_DEPTH[lowest];
}

/** The group a key renders under: the dotted prefix ("audio", "network"). */
export function groupOf(key: string): string {
  const dot = key.indexOf(".");
  return dot === -1 ? key : key.slice(0, dot);
}

export type RowProvenance = "inherited" | "overridden" | "edited" | "default" | "inventory";

/** The STATE chip for one row at one level: staged edits win, then an
 * override AT this level, then inherited-from-an-ancestor, then default;
 * inventory keys are their own state. */
export function provenanceOf(
  def: CatalogKey,
  effective: EffectiveKey | undefined,
  level: EntityLevel,
  overrides: OverrideMap,
  staged: Map<string, unknown>,
): RowProvenance {
  if (def.resolution === "inventory") {
    return "inventory";
  }
  if (staged.has(def.key)) {
    return "edited";
  }
  if (def.key in overrides) {
    return "overridden";
  }
  if (effective === undefined || effective.source === "default") {
    return "default";
  }
  return effective.source === level ? "overridden" : "inherited";
}

/** The ONE-PUT body: the server's sparse map with staged edits folded in
 * and reverted keys dropped (REVERT is a staged undefined). Secrets kept
 * untouched round-trip as the sentinel the redacted GET handed us. */
export const REVERT = Symbol("revert-override");

export function buildDraftPut(
  server: OverrideMap,
  staged: Map<string, unknown | typeof REVERT>,
): OverrideMap {
  const next: OverrideMap = { ...server };
  for (const [key, value] of staged) {
    if (value === REVERT) {
      delete next[key];
    } else {
      next[key] = value;
    }
  }
  return next;
}

/** Unit suffix derived from the key's tail - the catalog carries no unit
 * column (reconciliation #1); purely presentational. */
export function unitOf(key: string): string | null {
  if (key.endsWith("_seconds")) {
    return "s";
  }
  if (key.endsWith("_hz")) {
    return "Hz";
  }
  if (key.endsWith("_db")) {
    return "dB";
  }
  if (key.endsWith("_bytes")) {
    return "bytes";
  }
  return null;
}
