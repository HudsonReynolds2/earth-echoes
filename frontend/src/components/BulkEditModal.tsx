/**
 * The S4 bulk-edit modal (task E2.8; spec 5.2; D56, D58): a wide two-pane
 * variant of the ONE modal vocabulary. Left: the change form (catalog-driven
 * key picker and value editor, write-at-level with consequence copy) and the
 * impact grid — three live figures plus the "Offline now" slot that names E3
 * instead of inventing data (D40). Right: the server-computed preview table.
 *
 * COMMIT GATING (the acceptance): Commit stays disabled until the CURRENT
 * form deep-equals the payload the server last previewed; any change
 * re-disables it until re-preview. S4's "Publish immediately" checkbox is
 * replaced by one line of copy naming E3 — nothing here publishes (D58).
 */
import { useMutation } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  applyConfig,
  ApplyResult,
  CatalogKey,
  createSelection,
  DevicePlanItem,
  editableAt,
  ListEnvelope,
  PlanBody,
  PlanSelection,
  previewConfig,
} from "../lib/config";
import { CatalogEditor } from "./CatalogEditor";

type WriteLevel = "target" | "pod";

const LEVEL_COPY: Record<WriteLevel, string> = {
  target: "Writes one override per selected listener — each device keeps its own value.",
  pod: "Writes ONE override on the shared pod — every listener in it inherits, selected or not.",
};

export function BulkEditModal({
  open,
  onClose,
  selection,
  selectionLabel,
  catalog,
  canSaveSelection,
}: {
  open: boolean;
  onClose: () => void;
  selection: PlanSelection;
  selectionLabel: string;
  catalog: CatalogKey[];
  canSaveSelection: boolean;
}) {
  const writableKeys = useMemo(
    () =>
      catalog.filter(
        (def) => !def.secret && (editableAt(def, "listener") || editableAt(def, "pod")),
      ),
    [catalog],
  );
  const [keyName, setKeyName] = useState("");
  const [value, setValue] = useState<unknown>(undefined);
  const [level, setLevel] = useState<WriteLevel>("target");
  const [previewed, setPreviewed] = useState<{
    payload: string;
    result: ListEnvelope<DevicePlanItem>;
  } | null>(null);
  const [saveName, setSaveName] = useState("");
  const [savedAs, setSavedAs] = useState<string | null>(null);
  const [applied, setApplied] = useState<ApplyResult | null>(null);

  const def = writableKeys.find((item) => item.key === keyName);
  const levelOptions: WriteLevel[] = def
    ? (["target", "pod"] as const).filter((option) =>
        editableAt(def, option === "target" ? "listener" : "pod"),
      )
    : ["target"];
  const effectiveLevel = levelOptions.includes(level) ? level : levelOptions[0];

  const body: PlanBody | null =
    def !== undefined && value !== undefined
      ? { selection, changes: { [def.key]: value }, level: effectiveLevel }
      : null;
  const payloadNow = body ? JSON.stringify(body) : null;
  const previewCurrent = previewed !== null && previewed.payload === payloadNow;

  const preview = useMutation({
    mutationFn: (planBody: PlanBody) => previewConfig(planBody, { limit: 500 }),
    onSuccess: (result, planBody) => {
      setPreviewed({ payload: JSON.stringify(planBody), result });
    },
  });
  const apply = useMutation({
    mutationFn: (planBody: PlanBody) => applyConfig(planBody),
    onSuccess: (result) => setApplied(result),
  });
  const save = useMutation({
    mutationFn: () =>
      createSelection({
        name: saveName,
        query: selection as Exclude<PlanSelection, { selection_id: string }>,
      }),
    onSuccess: (row) => setSavedAs(row.name),
  });

  if (!open) {
    return null;
  }

  const items = previewCurrent ? previewed.result.items : [];
  const willChange = items.filter((item) => !item.no_op).length;

  return (
    <div className="modal-overlay" role="presentation">
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-label="Bulk edit configuration"
        data-testid="bulk-edit-modal"
      >
        <h2>Bulk edit · {selectionLabel}</h2>
        {applied ? (
          <ApplyOutcome result={applied} onClose={onClose} />
        ) : (
          <div className="modal-panes">
            <div className="modal-pane-form">
              <div className="form-field">
                <label htmlFor="bulk-key">Setting</label>
                <select
                  id="bulk-key"
                  value={keyName}
                  onChange={(event) => {
                    setKeyName(event.target.value);
                    setValue(undefined);
                  }}
                >
                  <option value="">Choose a setting…</option>
                  {writableKeys.map((item) => (
                    <option key={item.key} value={item.key}>
                      {item.key}
                    </option>
                  ))}
                </select>
              </div>
              {def && (
                <div className="form-field">
                  <label>New value</label>
                  <CatalogEditor
                    def={def}
                    value={value ?? def.default}
                    disabled={false}
                    onChange={setValue}
                  />
                </div>
              )}
              {def && (
                <div className="form-field">
                  <label htmlFor="bulk-level">Write at</label>
                  <select
                    id="bulk-level"
                    value={effectiveLevel}
                    onChange={(event) => setLevel(event.target.value as WriteLevel)}
                  >
                    {levelOptions.map((option) => (
                      <option key={option} value={option}>
                        {option === "target" ? "Each selected listener" : "Their shared pod"}
                      </option>
                    ))}
                  </select>
                  <p className="form-help">{LEVEL_COPY[effectiveLevel]}</p>
                </div>
              )}
              <div className="impact-grid" data-testid="impact-grid">
                <ImpactFigure
                  label="Matched"
                  value={previewCurrent ? previewed.result.total : "—"}
                />
                <ImpactFigure label="Will change" value={previewCurrent ? willChange : "—"} />
                <ImpactFigure
                  label="No-op"
                  value={previewCurrent ? previewed.result.total - willChange : "—"}
                />
                <ImpactFigure label="Offline now" value="—" caption="live status arrives with E3" />
              </div>
              <div className="form-actions">
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={body === null || preview.isPending}
                  data-testid="run-preview"
                  onClick={() => body && preview.mutate(body)}
                >
                  Preview
                </button>
                <button
                  type="button"
                  disabled={!previewCurrent || apply.isPending}
                  data-testid="commit-change"
                  onClick={() => body && apply.mutate(body)}
                >
                  Commit to {previewCurrent ? willChange : "…"} devices
                </button>
                <button type="button" className="btn-tertiary" onClick={onClose}>
                  Cancel
                </button>
              </div>
              <p className="muted">
                Revisions are created as drafts — publishing arrives with E3 (EOE_PUBLISH_ENABLED).
              </p>
              {canSaveSelection && !("selection_id" in selection) && (
                <div className="form-field">
                  <label htmlFor="save-selection-name">Save as selection</label>
                  <span className="save-selection-row">
                    <input
                      id="save-selection-name"
                      value={saveName}
                      onChange={(event) => setSaveName(event.target.value)}
                      placeholder="e.g. coastal listeners"
                    />
                    <button
                      type="button"
                      className="btn-tertiary"
                      disabled={saveName.trim() === "" || save.isPending}
                      onClick={() => save.mutate()}
                    >
                      Save
                    </button>
                  </span>
                  {savedAs && <p className="form-help">Saved as “{savedAs}”.</p>}
                  {save.isError && <p className="form-error">{(save.error as Error).message}</p>}
                </div>
              )}
              {(preview.isError || apply.isError) && (
                <p className="form-error" data-testid="bulk-error">
                  {((preview.error ?? apply.error) as Error).message}
                </p>
              )}
            </div>
            <div className="modal-pane-preview">
              <p className="eyebrow">
                Affected devices <span className="muted">computed server-side</span>
              </p>
              {previewCurrent ? (
                <div className="data-table-wrap">
                  <table className="data-table" data-testid="bulk-preview-table">
                    <thead>
                      <tr>
                        <th scope="col">Device</th>
                        <th scope="col">MAC / id</th>
                        <th scope="col">Pod</th>
                        <th scope="col">Current</th>
                        <th scope="col">Resulting</th>
                        {/* The E3 slot: the column exists, the data does not (D40). */}
                        <th scope="col">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((item) => (
                        <tr
                          key={`${item.target_type}-${item.target_id}`}
                          className={item.no_op ? "row-noop" : undefined}
                        >
                          <td>{item.name}</td>
                          <td className="cell-mono">{item.target_id}</td>
                          <td>{item.pod_name}</td>
                          <td className="cell-mono">{cell(item.before, def?.key)}</td>
                          <td className="cell-mono cell-resulting">{cell(item.after, def?.key)}</td>
                          <td className="muted">—</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="muted">Status data arrives with E3 — nothing is fabricated here.</p>
                </div>
              ) : (
                <p className="muted" data-testid="preview-stale">
                  {previewed
                    ? "The form changed since the last preview — run Preview again to re-enable Commit."
                    : "Run Preview to see the affected devices before anything can commit."}
                </p>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ImpactFigure({
  label,
  value,
  caption,
}: {
  label: string;
  value: number | string;
  caption?: string;
}) {
  return (
    <div className="impact-figure">
      <span className="impact-value mono">{value}</span>
      <span className="impact-label">{label}</span>
      {caption && <span className="impact-caption">{caption}</span>}
    </div>
  );
}

function ApplyOutcome({ result, onClose }: { result: ApplyResult; onClose: () => void }) {
  return (
    <div data-testid="apply-outcome">
      <p>
        Committed. {result.revisions.length} draft{" "}
        {result.revisions.length === 1 ? "revision" : "revisions"} created — state{" "}
        <span className="mono">{result.state}</span>; publishing arrives with E3
        {result.publish_enabled ? "" : " (EOE_PUBLISH_ENABLED is off)"}.
      </p>
      <ul className="apply-revisions">
        {result.revisions.slice(0, 8).map((revision) => (
          <li key={revision.revision_id} className="mono">
            {revision.target_id} · {revision.checksum.slice(0, 16)}…
          </li>
        ))}
      </ul>
      <div className="form-actions">
        <button type="button" onClick={onClose}>
          Done
        </button>
      </div>
    </div>
  );
}

function cell(config: Record<string, { value: unknown }>, key: string | undefined): string {
  if (!key || !(key in config)) {
    return "—";
  }
  const value = config[key].value;
  if (value === null || value === undefined) {
    return "—";
  }
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
