/**
 * Per-row provenance chip (task E2.7): the .outcome-* NON-status precedent —
 * its own data attribute (data-provenance), colored word chips, no glyphs.
 * NEVER StatusChip, never data-status: provenance is where a value came
 * from, not device health (D40 stays intact until E3).
 */
import { RowProvenance } from "../lib/config";

const LABELS: Record<RowProvenance, string> = {
  inherited: "inherited",
  overridden: "set here",
  edited: "edited",
  default: "default",
  inventory: "inventory",
};

export function RowStateChip({ provenance }: { provenance: RowProvenance }) {
  return (
    <span className="provenance-chip" data-provenance={provenance}>
      {LABELS[provenance]}
    </span>
  );
}
