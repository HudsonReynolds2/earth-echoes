/**
 * The S3 provenance table (task E2.7): one .data-table extension (the D42
 * single-vocabulary rule — additive .config-table classes, deliberately NOT
 * TanStack: rows group by key prefix, unsorted and unpaginated by design).
 * Five columns: KEY / VALUE / RESOLVED FROM / STATE / revert. Quiet
 * inherited rows, loud overridden ones; the revert control is U+00D7 (the
 * TagEditor precedent — no ↺ exists in the vendored fonts, D27).
 *
 * Level rule rendering (D50): a key is editable here iff this level is at
 * or above its lowest level; below-lowest rows render read-only with true
 * provenance. Inventory keys point at the listener page; service keys name
 * E5. Secret rows: SECRET chip, constant bullets, write-only Replace — no
 * reveal, no copy, and the diff says "replaced" (D51).
 */
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";

import {
  CatalogKey,
  EffectiveConfig,
  EntityLevel,
  groupOf,
  editableAt,
  isSecretSet,
  OverrideMap,
  provenanceOf,
  REVERT,
} from "../lib/config";
import { CatalogEditor } from "./CatalogEditor";
import { RowStateChip } from "./RowStateChip";

const GROUP_CAPTIONS: Record<string, string> = {
  audio: "capture format for every recording",
  capture: "when and how listeners record",
  listener: "listener supervision",
  buffering: "on-device fallback storage",
  logging: "diagnostics",
  network: "shared by every listener in a pod",
  identity: "who this device is (inventory-owned)",
  location: "where this device is (inventory-owned)",
  analysis: "edge AI on the aggregator",
  upload: "recording delivery",
  telemetry: "deployment service endpoints",
};

const SOURCE_LABELS: Record<string, string> = {
  organization: "Organization",
  deployment: "Deployment",
  pod: "Pod",
  aggregator: "Aggregator",
  listener: "Listener",
  default: "default",
  inventory: "inventory",
};

export function ProvenanceTable({
  catalog,
  effective,
  overrides,
  staged,
  level,
  canEdit,
  onlyOverridden,
  errors,
  onStage,
  onRevert,
}: {
  catalog: CatalogKey[];
  effective: EffectiveConfig;
  overrides: OverrideMap;
  staged: Map<string, unknown | typeof REVERT>;
  level: EntityLevel;
  canEdit: boolean;
  onlyOverridden: boolean;
  errors: Record<string, string>;
  onStage: (key: string, value: unknown) => void;
  onRevert: (key: string) => void;
}) {
  // Only keys the merge resolves at this level render (inventory keys exist
  // at listener level only — the effective map is the truth).
  const rows = catalog.filter((def) => def.key in effective.config);
  const groups = [...new Set(rows.map((def) => groupOf(def.key)))];
  return (
    <div className="data-table-wrap">
      <table className="data-table config-table" data-testid="provenance-table">
        <thead>
          <tr>
            <th scope="col">Key</th>
            <th scope="col">Value</th>
            <th scope="col">Resolved from</th>
            <th scope="col">State</th>
            <th scope="col">
              <span className="visually-hidden">Revert</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => {
            const members = rows.filter((def) => groupOf(def.key) === group);
            const visible = members.filter((def) => {
              if (!onlyOverridden) {
                return true;
              }
              const provenance = provenanceOf(
                def,
                effective.config[def.key],
                level,
                overrides,
                staged,
              );
              return provenance === "overridden" || provenance === "edited";
            });
            if (visible.length === 0) {
              return null;
            }
            return (
              <Fragment key={group}>
                <tr className="config-group">
                  <th colSpan={5} scope="colgroup">
                    <span className="config-group-name">{group}</span>
                    {GROUP_CAPTIONS[group] && (
                      <span className="config-group-caption">{GROUP_CAPTIONS[group]}</span>
                    )}
                  </th>
                </tr>
                {visible.map((def) => (
                  <ConfigRow
                    key={def.key}
                    def={def}
                    effective={effective}
                    overrides={overrides}
                    staged={staged}
                    level={level}
                    canEdit={canEdit}
                    error={errors[def.key]}
                    onStage={onStage}
                    onRevert={onRevert}
                  />
                ))}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ConfigRow({
  def,
  effective,
  overrides,
  staged,
  level,
  canEdit,
  error,
  onStage,
  onRevert,
}: {
  def: CatalogKey;
  effective: EffectiveConfig;
  overrides: OverrideMap;
  staged: Map<string, unknown | typeof REVERT>;
  level: EntityLevel;
  canEdit: boolean;
  error?: string;
  onStage: (key: string, value: unknown) => void;
  onRevert: (key: string) => void;
}) {
  const resolved = effective.config[def.key];
  const provenance = provenanceOf(def, resolved, level, overrides, staged);
  const editable = canEdit && editableAt(def, level);
  const stagedValue = staged.get(def.key);
  const showValue = staged.has(def.key) && stagedValue !== REVERT ? stagedValue : resolved?.value;
  const loud = provenance === "overridden" || provenance === "edited";
  return (
    <tr className={loud ? "row-overridden" : undefined} data-key={def.key}>
      <td className="config-key">
        <span className="mono">{def.key}</span>
        {def.secret && <span className="secret-chip">secret</span>}
      </td>
      <td className="config-value">
        <ValueCell
          def={def}
          value={showValue}
          editable={editable}
          staged={staged.has(def.key)}
          onStage={onStage}
        />
        {def.write_restricted !== null && (
          <p className="config-cell-note">Managed by services onboarding — arrives with E5.</p>
        )}
        {def.resolution === "inventory" && (
          <p className="config-cell-note">
            Inventory-owned — edited on{" "}
            <Link to={`/inventory/listeners/${encodeURIComponent(effective.entity_id)}`}>
              the listener page
            </Link>
            .
          </p>
        )}
        {error && <p className="form-error">{error}</p>}
      </td>
      <td className="config-source">{SOURCE_LABELS[resolved?.source ?? "default"]}</td>
      <td className="config-state">
        <RowStateChip provenance={provenance} />
      </td>
      <td className="config-revert">
        {editable && (provenance === "overridden" || provenance === "edited") && (
          <button
            type="button"
            className="config-revert-btn"
            aria-label={`Remove override ${def.key}`}
            onClick={() => onRevert(def.key)}
          >
            ×
          </button>
        )}
      </td>
    </tr>
  );
}

function ValueCell({
  def,
  value,
  editable,
  staged,
  onStage,
}: {
  def: CatalogKey;
  value: unknown;
  editable: boolean;
  staged: boolean;
  onStage: (key: string, value: unknown) => void;
}) {
  const [replacing, setReplacing] = useState(false);
  if (def.secret) {
    const set = isSecretSet(value) || (staged && typeof value === "string");
    return (
      <span className="secret-value">
        <span className="mono" aria-hidden="true">
          ••••••••
        </span>
        <span className="config-cell-note">{set ? "set" : "not set"}</span>
        {editable && !replacing && (
          <button type="button" className="btn-tertiary" onClick={() => setReplacing(true)}>
            Replace
          </button>
        )}
        {editable && replacing && (
          <input
            type="password"
            aria-label={`Replacement value for ${def.key}`}
            placeholder="new value — write-only"
            autoFocus
            onChange={(event) => onStage(def.key, event.target.value)}
          />
        )}
      </span>
    );
  }
  if (!editable) {
    return (
      <span className="config-readonly mono">
        {value === null || value === undefined ? "—" : formatValue(value)}
      </span>
    );
  }
  return (
    <CatalogEditor
      def={def}
      value={value}
      disabled={false}
      onChange={(next) => onStage(def.key, next)}
    />
  );
}

function formatValue(value: unknown): string {
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}
