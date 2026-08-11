/**
 * The per-device reconciliation timeline (task E3.11; spec 6.3).
 *
 * Renders `reconciliation_event` rows: what state the revision moved to, what
 * moved it, who, and what changed. One component for both device kinds,
 * because a Listener's history and an Aggregator's answer the same question.
 *
 * **`data-revision-state`, deliberately not `data-status`.** A revision state
 * (spec 6.2) and a device status (spec 9.3) are different vocabularies — one
 * describes a config change, the other whether the hardware is reachable.
 * They are also under different honesty rules: D40 forbids any `[data-status]`
 * on inventory routes until E3.12 has REAL device status to put there, and
 * borrowing that attribute for revision states would defeat a guard that
 * exists to stop exactly this kind of plausible-looking placeholder.
 */
import { useQuery } from "@tanstack/react-query";

import {
  actorLabel,
  diffRows,
  fetchTimeline,
  renderValue,
  TimelineEntry,
  TimelineTarget,
  triggerLabel,
} from "../lib/timeline";

const PAGE = 20;

function when(iso: string): string {
  return new Date(iso).toLocaleString();
}

function Entry({ entry }: { entry: TimelineEntry }) {
  const rows = diffRows(entry.diff);
  const detail = entry.detail ?? {};
  const keys = Array.isArray(detail.differing_keys) ? (detail.differing_keys as string[]) : [];
  return (
    <li className="timeline-entry" data-revision-state={entry.to_state}>
      <div className="timeline-head">
        <span className="timeline-state mono">{entry.to_state}</span>
        <span className="timeline-trigger">{triggerLabel(entry.trigger)}</span>
        <time dateTime={entry.at} className="muted">
          {when(entry.at)}
        </time>
      </div>
      <p className="muted timeline-actor">
        {entry.from_state} → {entry.to_state} · by {actorLabel(entry)}
      </p>
      {rows.length > 0 && (
        <table className="timeline-diff">
          <thead>
            <tr>
              <th scope="col">Setting</th>
              <th scope="col">Before</th>
              <th scope="col">After</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([key, change]) => (
              <tr key={key}>
                <td className="mono">{key}</td>
                <td className="mono">{renderValue(change.before)}</td>
                <td className="mono">{renderValue(change.after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {keys.length > 0 && (
        <p className="timeline-detail">
          Device disagreed on: <span className="mono">{keys.join(", ")}</span>
        </p>
      )}
      {typeof detail.error === "string" && <p className="timeline-detail">{detail.error}</p>}
    </li>
  );
}

export function DeviceTimeline({ target }: { target: TimelineTarget }) {
  const key = target.kind === "aggregator" ? target.id : target.mac;
  const timeline = useQuery({
    queryKey: ["timeline", target.kind, key],
    queryFn: () => fetchTimeline(target, { limit: PAGE }),
  });

  if (timeline.isError) {
    return (
      <section className="card" data-testid="device-timeline">
        <h2>Timeline</h2>
        <p className="muted">The timeline could not be loaded.</p>
      </section>
    );
  }
  if (!timeline.data) {
    return (
      <section className="card" data-testid="device-timeline">
        <h2>Timeline</h2>
        <p className="muted">Loading…</p>
      </section>
    );
  }

  const { items, total } = timeline.data;
  return (
    <section className="card" data-testid="device-timeline">
      <h2>Timeline</h2>
      {items.length === 0 ? (
        // Never transitioned is a real answer and a common one: a device that
        // has been entered in inventory but never had config published has no
        // history, and saying so is better than an empty box.
        <p className="muted" data-testid="timeline-empty">
          No configuration has been published to this device yet.
        </p>
      ) : (
        <>
          <ol className="timeline">
            {items.map((entry) => (
              <Entry key={entry.id} entry={entry} />
            ))}
          </ol>
          {total > items.length && (
            <p className="muted">
              Showing the {items.length} most recent of {total}.
            </p>
          )}
        </>
      )}
    </section>
  );
}
