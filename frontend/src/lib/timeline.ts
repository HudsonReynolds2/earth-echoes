/**
 * Reconciliation timeline client (task E3.11; spec 6.3, 6.2).
 *
 * One function per call, plus the PURE presentation helpers the panel leans
 * on, kept here so they are unit-testable without rendering: what a
 * transition should be CALLED in a sentence, and how a config diff reads.
 *
 * The vocabulary is deliberately not invented here. `to_state` is spec 6.2's
 * and `trigger` is its Trigger column, both arriving as strings the backend
 * already validated; this module maps them to English and refuses to
 * paraphrase what it does not recognize.
 */
import { ListEnvelope, query, request } from "./http";

export type RevisionState = "draft" | "pending" | "applied" | "drifted" | "failed" | "superseded";

export interface DiffEntry {
  before: unknown;
  after: unknown;
}

export interface TimelineEntry {
  id: string;
  at: string;
  revision_id: string;
  from_state: string;
  to_state: string;
  trigger: string;
  actor_user_id: string | null;
  /** Null for system-driven moves. Not "unknown" — see `actorLabel`. */
  actor_email: string | null;
  /** Platform side: snapshot vs snapshot, so values here are safe to show. */
  diff: Record<string, DiffEntry> | null;
  /** Device or worker side: key NAMES and error text, never device values. */
  detail: Record<string, unknown> | null;
}

export type TimelineTarget = { kind: "aggregator"; id: string } | { kind: "listener"; mac: string };

export function fetchTimeline(
  target: TimelineTarget,
  params: { limit?: number; offset?: number } = {},
): Promise<ListEnvelope<TimelineEntry>> {
  const path =
    target.kind === "aggregator"
      ? `/aggregators/${target.id}/timeline`
      : `/listeners/${encodeURIComponent(target.mac)}/timeline`;
  return request<ListEnvelope<TimelineEntry>>(`${path}${query(params)}`);
}

/**
 * What moved the revision, in words. Spec 6.2's Trigger column exists because
 * `failed` alone cannot tell an operator whether the device rejected the
 * config or never answered — and those call for opposite responses, so the
 * distinction has to survive into the sentence they read.
 */
const TRIGGER_TEXT: Record<string, string> = {
  publish: "published",
  report_match: "applied by the device",
  report_error: "rejected by the device",
  timeout: "timed out with no reply",
  report_diverged: "diverged from desired",
  republish: "re-published",
  retry: "retried",
  newer_revision: "replaced by a newer revision",
};

export function triggerLabel(trigger: string): string {
  // An unrecognized trigger is shown verbatim rather than guessed at. A new
  // one means the backend grew a spec 6.2 edge this file has not learned.
  return TRIGGER_TEXT[trigger] ?? trigger;
}

/**
 * Who did it. Null means the SYSTEM did — a timeout, a device report, a drift
 * sweep — and that is a fact, not a gap. Rendering it as "unknown" would
 * suggest the platform lost track of a person who was never involved.
 */
export function actorLabel(entry: TimelineEntry): string {
  if (entry.actor_email) return entry.actor_email;
  if (entry.actor_user_id) return "a deleted user";
  return "the platform";
}

/** Rows for the diff table: stable key order so a re-render never reshuffles. */
export function diffRows(diff: Record<string, DiffEntry> | null): Array<[string, DiffEntry]> {
  if (!diff) return [];
  return Object.entries(diff).sort(([a], [b]) => a.localeCompare(b));
}

/** A config value as one short cell. `undefined`/absent reads as "unset". */
export function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "unset";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
