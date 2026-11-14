/**
 * The bundle tracking board (task E4.10; spec 8.5; screen S6).
 *
 * "A board, not a table, because the five provisioning statuses are a
 * pipeline a human physically walks through: cards get written, devices come
 * online, reports get compared" (S6). One column per spec 8.5 status, in
 * pipeline order. The header carries the two field actions: download the
 * archive, and mark the cards that are still `generated` as flashed, the one
 * transition a person asserts (D179). Every other column fills in by itself
 * as the worker's reconciler reads what devices report.
 */
import { RecordChip } from "./RecordChip";
import {
  BundleDetail,
  ProvisioningRecord,
  RECORD_STATUSES,
  RecordStatus,
} from "../lib/provisioning";

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
  listenerNames,
  canManage,
  marking,
  downloading,
  notice,
  onMarkFlashed,
  onDownload,
}: {
  bundle: BundleDetail;
  podNames: Record<string, string>;
  listenerNames: Record<string, string>;
  canManage: boolean;
  marking: boolean;
  downloading: boolean;
  notice: string | null;
  onMarkFlashed: (recordIds: string[]) => void;
  onDownload: () => void;
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
  const pending = byStatus.generated.map((record) => record.id);

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
            <button
              type="button"
              onClick={() => onMarkFlashed(pending)}
              disabled={pending.length === 0 || marking}
              data-testid="mark-flashed"
            >
              {marking
                ? "Marking…"
                : `Mark ${pending.length} card${pending.length === 1 ? "" : "s"} flashed`}
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
            <ul className="bundle-cards">
              {byStatus[status].map((record) => (
                <RecordCard key={record.id} record={record} listenerNames={listenerNames} />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </section>
  );
}

function RecordCard({
  record,
  listenerNames,
}: {
  record: ProvisioningRecord;
  listenerNames: Record<string, string>;
}) {
  const last = record.status_history[record.status_history.length - 1];
  const isAggregator = record.target_type === "aggregator";
  return (
    <li className="bundle-card" data-testid={`record-${record.target_id}`}>
      <strong>
        {isAggregator ? "Aggregator" : (listenerNames[record.target_id] ?? "Listener")}
      </strong>
      <span className="mono">{isAggregator ? record.aggregator_uuid : record.target_id}</span>
      <span className="form-help">
        {record.file_path.split("/").slice(2).join("/")}
        {last ? ` · ${new Date(last.at).toLocaleTimeString()}` : ""}
      </span>
    </li>
  );
}
