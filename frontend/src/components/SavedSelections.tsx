/**
 * The saved-selections rail block (task E2.8; S3's rail slot via the
 * HierarchyTree footer prop). Lists GET /selections; clicking one seeds the
 * bulk modal by reference, so the server re-evaluates membership at use
 * (D54 — never a stale id list). No delete affordance: spec 13 ships
 * selections as GET/POST only (D54, reconciled and recorded).
 */
import { useQuery } from "@tanstack/react-query";

import { listSelections, SavedSelection } from "../lib/config";

export function SavedSelections({ onOpen }: { onOpen: (selection: SavedSelection) => void }) {
  const selections = useQuery({ queryKey: ["selections"], queryFn: listSelections });
  return (
    <div className="rail-block" data-testid="saved-selections">
      <p className="eyebrow">Saved selections</p>
      {selections.data && selections.data.total > 0 ? (
        <ul className="rail-block-list">
          {selections.data.items.map((selection) => (
            <li key={selection.id}>
              <button type="button" className="rail-block-link" onClick={() => onOpen(selection)}>
                {selection.name}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">None yet — save one from a bulk edit.</p>
      )}
    </div>
  );
}
