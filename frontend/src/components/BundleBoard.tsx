/**
 * The bundle tracking board (task E4.10; spec 8.5; screen S6).
 *
 * "A board, not a table, because the five provisioning statuses are a
 * pipeline a human physically walks through: cards get written, devices come
 * online, reports get compared" (S6). One column per spec 8.5 status, in
 * pipeline order. The header carries the two field actions: download the
 * archive. The two columns a person drives carry their own action: check
 * cards in Generated and mark them flashed, the one transition a person
 * asserts (D179), or check cards in Flashed and unmark a mis-click (D187).
 * Cards are picked one by one because a field tech writes 30 cards over hours,
 * not in one sitting: each card's status is saved on its own record as it is
 * marked, so the board is the running tally of which cards are done (D184).
 * Every other column fills in by itself as the worker's reconciler reads what
 * devices report.
 */
import { FormEvent, useState } from "react";

import { RecordChip } from "./RecordChip";
import { Listener } from "../lib/inventory";
import {
  BundleDetail,
  OperatorStatus,
  ProvisioningRecord,
  RECORD_STATUSES,
  RecordStatus,
} from "../lib/provisioning";

/** The two columns a person drives, and what checking cards in each does. */
const COLUMN_ACTION: Partial<Record<RecordStatus, OperatorStatus>> = {
  generated: "flashed",
  flashed: "generated",
};

const COLUMN_HELP: Record<RecordStatus, string> = {
  generated: "Card file prepared, not yet written.",
  flashed: "Card written; waiting for the device to come online.",
  registered: "Device online and matched; its config differs from the card's.",
  confirmed: "Device reported exactly the config on its card.",
  mismatch: "Device reported an unexpected identity or config.",
};

export function BundleBoard({
  bundle,
  podNames,
  listeners,
  canManage,
  canEditDevices,
  marking,
  downloading,
  notice,
  onSetStatus,
  onDownload,
  onFillIn,
}: {
  bundle: BundleDetail;
  podNames: Record<string, string>;
  listeners: Record<string, Listener>;
  canManage: boolean;
  canEditDevices: boolean;
  marking: boolean;
  downloading: boolean;
  notice: string | null;
  onSetStatus: (recordIds: string[], to: OperatorStatus) => void;
  onDownload: () => void;
  onFillIn: (mac: string, input: FillIn) => Promise<void>;
}) {
  const byStatus = Object.fromEntries(
    RECORD_STATUSES.map((status) => [
      status,
      bundle.records.filter((record) => record.status === status),
    ]),
  ) as Record<RecordStatus, ProvisioningRecord[]>;
  const files = new Set(bundle.records.map((record) => record.file_path)).size;
  const total = bundle.records.length;
  const pods = Object.keys(bundle.plan)
    .map((id) => podNames[id] ?? id)
    .join(", ");
  // The checked cards. A column's action counts only its own cards, so a card
  // that moved on (marked here, or by the reconciler on a refetch) drops out.
  const [checked, setChecked] = useState<ReadonlySet<string>>(new Set());
  const toggle = (id: string) =>
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <section className="bundle-board" data-testid="bundle-board" aria-label="Bundle tracking board">
      <header className="bundle-board-head">
        <div>
          <p className="eyebrow">
            Bundle <span className="mono">{bundle.id.slice(0, 8)}</span>
          </p>
          <h2>{pods}</h2>
          <p className="muted">
            {bundle.scope_type === "pod" ? "Pod bundle" : "Deployment bundle"} · generated{" "}
            {new Date(bundle.generated_at).toLocaleString()} · card format v
            {bundle.template_version} (draft)
          </p>
        </div>
        {canManage && (
          <div className="page-header-actions">
            <button
              type="button"
              className="btn-secondary"
              onClick={onDownload}
              disabled={downloading}
            >
              {downloading ? "Downloading…" : "Download bundle"}
            </button>
          </div>
        )}
      </header>
      {notice && (
        <p className="bundle-board-notice" role="status">
          {notice}
        </p>
      )}

      <dl className="bundle-tiles">
        <div className="bundle-tile">
          <dt>Files in bundle</dt>
          <dd>{files}</dd>
        </div>
        <div className="bundle-tile">
          <dt>Cards expected</dt>
          <dd>{total}</dd>
        </div>
        <div className="bundle-tile">
          <dt>Confirmed</dt>
          <dd>
            {byStatus.confirmed.length} / {total}
          </dd>
        </div>
        <div className="bundle-tile" data-alert={byStatus.mismatch.length > 0 ? "true" : undefined}>
          <dt>Mismatch</dt>
          <dd>{byStatus.mismatch.length}</dd>
        </div>
      </dl>

      <div className="bundle-columns">
        {RECORD_STATUSES.map((status) => (
          <section
            key={status}
            className="bundle-column"
            aria-label={`${status} cards`}
            data-testid={`column-${status}`}
          >
            <header className="bundle-column-head">
              <RecordChip status={status} />
              <span className="bundle-column-count">{byStatus[status].length}</span>
            </header>
            <p className="form-help">{COLUMN_HELP[status]}</p>
            {canManage && COLUMN_ACTION[status] && byStatus[status].length > 0 && (
              <ColumnAction
                ids={byStatus[status].map((record) => record.id)}
                to={COLUMN_ACTION[status]}
                checked={checked}
                marking={marking}
                setChecked={setChecked}
                onRun={onSetStatus}
              />
            )}
            <ul className="bundle-cards">
              {byStatus[status].map((record) => (
                <RecordCard
                  key={record.id}
                  record={record}
                  listener={listeners[record.target_id]}
                  canEditDevices={canEditDevices}
                  selectable={canManage && COLUMN_ACTION[status] !== undefined}
                  checked={checked.has(record.id)}
                  disabled={marking}
                  onToggle={() => toggle(record.id)}
                  onFillIn={onFillIn}
                />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </section>
  );
}

/**
 * *Select all* plus the column's one button, acting on the checked cards of
 * this column only. "Mark 2 cards flashed" in Generated, "Unmark 2 cards" in
 * Flashed; disabled, and saying what to do, while nothing is checked.
 */
function ColumnAction({
  ids,
  to,
  checked,
  marking,
  setChecked,
  onRun,
}: {
  ids: string[];
  to: OperatorStatus;
  checked: ReadonlySet<string>;
  marking: boolean;
  setChecked: (next: ReadonlySet<string>) => void;
  onRun: (recordIds: string[], to: OperatorStatus) => void;
}) {
  const selected = ids.filter((id) => checked.has(id));
  const all = selected.length === ids.length;
  const noun = `card${selected.length === 1 ? "" : "s"}`;
  const label =
    selected.length === 0
      ? `Check cards to ${to === "flashed" ? "mark flashed" : "unmark"}`
      : to === "flashed"
        ? `Mark ${selected.length} ${noun} flashed`
        : `Unmark ${selected.length} ${noun}`;
  return (
    <div className="bundle-column-action">
      <label className="bundle-select-all">
        <input
          type="checkbox"
          checked={all}
          onChange={() => {
            const next = new Set(checked);
            for (const id of ids) {
              if (all) next.delete(id);
              else next.add(id);
            }
            setChecked(next);
          }}
          disabled={marking}
        />
        Select all
      </label>
      <button
        type="button"
        className={to === "flashed" ? undefined : "btn-secondary"}
        onClick={() => {
          onRun(selected, to);
          const next = new Set(checked);
          for (const id of selected) next.delete(id);
          setChecked(next);
        }}
        disabled={selected.length === 0 || marking}
        data-testid={to === "flashed" ? "mark-flashed" : "unmark-flashed"}
      >
        {marking ? "Saving…" : label}
      </button>
    </div>
  );
}

export interface FillIn {
  name: string;
  gps_lat: number | null;
  gps_lon: number | null;
}

/** A device that has spoken can be named and placed (spec 8.6, E4.11). */
const ONLINE = new Set(["registered", "confirmed"]);

function RecordCard({
  record,
  listener,
  canEditDevices,
  selectable,
  checked,
  disabled,
  onToggle,
  onFillIn,
}: {
  record: ProvisioningRecord;
  listener: Listener | undefined;
  canEditDevices: boolean;
  selectable: boolean;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
  onFillIn: (mac: string, input: FillIn) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const last = record.status_history[record.status_history.length - 1];
  const isAggregator = record.target_type === "aggregator";
  const needsGps = !isAggregator && listener !== undefined && listener.gps_lat === null;
  const canFill =
    !isAggregator && listener !== undefined && canEditDevices && ONLINE.has(record.status);
  return (
    <li
      className="bundle-card"
      data-testid={`record-${record.target_id}`}
      data-checked={checked || undefined}
    >
      {selectable ? (
        <label className="bundle-card-select">
          <input type="checkbox" checked={checked} onChange={onToggle} disabled={disabled} />
          <strong>{isAggregator ? "Aggregator" : (listener?.name ?? "Listener")}</strong>
        </label>
      ) : (
        <strong>{isAggregator ? "Aggregator" : (listener?.name ?? "Listener")}</strong>
      )}
      <span className="mono">{isAggregator ? record.aggregator_uuid : record.target_id}</span>
      <span className="form-help">
        {record.file_path.split("/").slice(2).join("/")}
        {last ? ` · ${new Date(last.at).toLocaleTimeString()}` : ""}
      </span>
      {needsGps && <span className="bundle-card-hint">No GPS yet</span>}
      {canFill && !editing && (
        <button type="button" className="btn-tertiary" onClick={() => setEditing(true)}>
          Fill in details
        </button>
      )}
      {canFill && editing && listener && (
        <FillInForm
          listener={listener}
          onCancel={() => setEditing(false)}
          onSave={async (input) => {
            await onFillIn(listener.mac, input);
            setEditing(false);
          }}
        />
      )}
    </li>
  );
}

/**
 * Name and GPS for a registered Listener (spec 8.6). It writes through E1's
 * `PATCH /listeners/{mac}`, the one write path for inventory (phase-4 E4.11),
 * so the inventory rules apply unchanged, name uniqueness included.
 */
function FillInForm({
  listener,
  onSave,
  onCancel,
}: {
  listener: Listener;
  onSave: (input: FillIn) => Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(listener.name);
  const [lat, setLat] = useState(listener.gps_lat === null ? "" : String(listener.gps_lat));
  const [lon, setLon] = useState(listener.gps_lon === null ? "" : String(listener.gps_lon));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const parse = (value: string) => (value.trim() === "" ? null : Number(value));
    const gps_lat = parse(lat);
    const gps_lon = parse(lon);
    if ((gps_lat === null) !== (gps_lon === null)) {
      setError("Give both latitude and longitude, or neither.");
      return;
    }
    if (
      (gps_lat !== null && Number.isNaN(gps_lat)) ||
      (gps_lon !== null && Number.isNaN(gps_lon))
    ) {
      setError("Latitude and longitude must be numbers.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onSave({ name: name.trim(), gps_lat, gps_lon });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="form bundle-fill-in"
      onSubmit={submit}
      aria-label={`Details for ${listener.mac}`}
    >
      <div className="form-field">
        <label htmlFor={`name-${listener.mac}`}>Name</label>
        <input
          id={`name-${listener.mac}`}
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
      </div>
      <div className="form-field">
        <label htmlFor={`lat-${listener.mac}`}>Latitude</label>
        <input id={`lat-${listener.mac}`} value={lat} onChange={(e) => setLat(e.target.value)} />
      </div>
      <div className="form-field">
        <label htmlFor={`lon-${listener.mac}`}>Longitude</label>
        <input id={`lon-${listener.mac}`} value={lon} onChange={(e) => setLon(e.target.value)} />
      </div>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="form-actions">
        <button type="button" className="btn-tertiary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="submit" disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  );
}
