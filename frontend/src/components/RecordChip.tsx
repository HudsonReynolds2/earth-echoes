/**
 * A provisioning record's status (task E4.10; spec 8.5).
 *
 * **Deliberately NOT `StatusChip`.** A record says where a CARD is in its
 * journey, not how a DEVICE is doing, and rendering one through the other is
 * the conflation D40 exists to stop. Color, glyph and word, the same three
 * channels `.service-chip` uses, in this vocabulary's own terms. The colors
 * are aliases of the shared status palette (tokens.ext.css), so green means
 * good here as everywhere, and the night theme comes for free.
 */
import { RecordStatus } from "../lib/provisioning";

const PRESENTATION: Record<RecordStatus, { glyph: string; word: string }> = {
  generated: { glyph: "○", word: "Generated" },
  flashed: { glyph: "↧", word: "Flashed" },
  registered: { glyph: "◎", word: "Registered" },
  confirmed: { glyph: "✓", word: "Confirmed" },
  mismatch: { glyph: "✕", word: "Mismatch" },
};

export function recordWord(status: RecordStatus): string {
  return PRESENTATION[status].word;
}

export function RecordChip({ status }: { status: RecordStatus }) {
  const { glyph, word } = PRESENTATION[status];
  return (
    <span className="record-chip" data-record-status={status}>
      <span aria-hidden="true">{glyph}</span>
      {word}
    </span>
  );
}
