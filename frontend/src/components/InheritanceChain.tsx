/**
 * The inheritance-chain ladder (task E2.7): the current branch of the
 * hierarchy, root to this entity, with live override counts for the self
 * and ancestor rungs (cheap cached queries — same keys as the editor).
 * Descendant rungs show STRUCTURAL counts only; the shadowing footnote and
 * per-descendant key counts are deferred with the owner until a chain-
 * summary endpoint exists (reconciliation #8 — no chain-summary in E2).
 */
import { useQueries } from "@tanstack/react-query";

import { ENTITY_PATHS, EntityLevel, getOverrides } from "../lib/config";

export interface ChainRung {
  level: EntityLevel;
  id: string;
  label: string;
}

export function InheritanceChain({
  rungs,
  current,
  descendantsNote,
}: {
  rungs: ChainRung[];
  current: EntityLevel;
  descendantsNote?: string;
}) {
  const counts = useQueries({
    queries: rungs.map((rung) => ({
      queryKey: ["config", "overrides", ENTITY_PATHS[rung.level], rung.id],
      queryFn: () => getOverrides(ENTITY_PATHS[rung.level], rung.id),
    })),
  });
  return (
    <section className="card chain-card" data-testid="inheritance-chain">
      <h2>Inheritance</h2>
      <ol className="chain">
        {rungs.map((rung, index) => {
          const overrides = counts[index]?.data?.overrides ?? {};
          const count = Object.keys(overrides).length;
          return (
            <li
              key={`${rung.level}-${rung.id}`}
              className="chain-rung"
              data-current={rung.level === current || undefined}
            >
              <span className="chain-level">{rung.level}</span>
              <span className="chain-label">{rung.label}</span>
              <span className="chain-count mono">
                {count} {count === 1 ? "override" : "overrides"}
              </span>
            </li>
          );
        })}
      </ol>
      {descendantsNote && <p className="muted">{descendantsNote}</p>}
    </section>
  );
}
