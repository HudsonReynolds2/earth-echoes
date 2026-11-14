/**
 * What is not on the map, and where to fix it (task E6.2b; D189).
 *
 * There is no guessed position for a Deployment or Pod without coordinates,
 * so it is listed here instead of drawn. An operator who can manage devices
 * gets a link straight to the form that places it; everyone else sees what is
 * missing, which is still worth knowing.
 */
import { Link } from "react-router-dom";

import { useCan } from "../Can";

export interface UnplacedItem {
  key: string;
  name: string;
  /** "deployment" or "pod": what the operator is placing. */
  kind: "deployment" | "pod";
  /** The inventory page whose Map location card places it. */
  fixAt: string;
  deploymentId: string;
  note?: string;
}

export function UnplacedPanel({ items }: { items: UnplacedItem[] }) {
  if (items.length === 0) return null;
  return (
    <section className="map-control map-unplaced" data-testid="map-unplaced" aria-label="Unplaced">
      <span className="eyebrow">Not on the map · {items.length}</span>
      <ul>
        {items.map((item) => (
          <UnplacedRow key={item.key} item={item} />
        ))}
      </ul>
    </section>
  );
}

function UnplacedRow({ item }: { item: UnplacedItem }) {
  const canFix = useCan("manage_devices", item.deploymentId);
  return (
    <li>
      <span>
        {item.name}
        {item.note && <span className="muted"> · {item.note}</span>}
      </span>
      {canFix ? (
        <Link to={item.fixAt}>Set location</Link>
      ) : (
        <span className="muted">No coordinates</span>
      )}
    </li>
  );
}
