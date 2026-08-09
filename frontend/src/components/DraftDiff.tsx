/**
 * Draft banner + diff rail (task E2.7). The banner counts UNSAVED staged
 * keys and its copy stays literally true in the write-through world:
 * nothing reaches devices until a revision publishes, and publishing is
 * E3's. The diff renders old → new with a CSS-drawn arrow (no → glyph in
 * the vendored fonts, D27); secret entries say "replaced", never values
 * (the mockup's DEK-rewrap copy is E3 §8.7 semantics — not rendered).
 */
import { CatalogKey, REVERT } from "../lib/config";

export function DraftBanner({ count, onViewDiff }: { count: number; onViewDiff?: () => void }) {
  if (count === 0) {
    return null;
  }
  return (
    <div className="draft-banner" data-testid="draft-banner" role="status">
      <strong>Unsaved draft</strong> — {count} {count === 1 ? "key" : "keys"} changed. Nothing
      reaches devices until you publish.
      {onViewDiff && (
        <button type="button" className="btn-tertiary" onClick={onViewDiff}>
          View diff
        </button>
      )}
    </div>
  );
}

export interface DiffEntry {
  key: string;
  old: unknown;
  next: unknown | typeof REVERT;
  secret: boolean;
}

export function DraftDiff({
  entries,
  byKey,
}: {
  entries: DiffEntry[];
  byKey: Record<string, CatalogKey>;
}) {
  if (entries.length === 0) {
    return <p className="muted">No unsaved changes.</p>;
  }
  return (
    <ul className="draft-diff" data-testid="draft-diff">
      {entries.map((entry) => (
        <li key={entry.key}>
          <span className="mono diff-key">
            {entry.key}
            {byKey[entry.key]?.secret && <span className="secret-chip">secret</span>}
          </span>
          <span className="diff-values">
            {entry.secret ? (
              <span className="diff-new">replaced</span>
            ) : (
              <>
                <span className="diff-old">{format(entry.old)}</span>
                <span className="diff-arrow" aria-hidden="true" />
                <span className="diff-new">
                  {entry.next === REVERT ? "inherited again" : format(entry.next)}
                </span>
              </>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

function format(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
