/**
 * The S3 hierarchy rail at v2 values (task E1.8): 36px rows, 14px indent per
 * level, weight ladder by kind, mono aggregator labels, right-aligned mono
 * child counts. Selection tracks the route (NavLink sets aria-current).
 *
 * NO status dots: E1 renders structure and counts only — reported state
 * arrives with E3 (DECISIONS: no fabricated status). The caret is CSS-drawn;
 * the ▶/▼ characters exist in no vendored font (D27).
 */
import { CSSProperties, useState } from "react";
import { NavLink } from "react-router-dom";

export interface TreeNode {
  id: string;
  kind: "organization" | "deployment" | "pod" | "aggregator";
  label: string;
  count?: number;
  to: string;
  children?: TreeNode[];
}

function matches(node: TreeNode, needle: string): boolean {
  if (node.label.toLowerCase().includes(needle)) {
    return true;
  }
  return (node.children ?? []).some((child) => matches(child, needle));
}

function TreeRow({ node, depth, filter }: { node: TreeNode; depth: number; filter: string }) {
  const [expanded, setExpanded] = useState(true);
  if (filter && !matches(node, filter)) {
    return null;
  }
  const children = node.children ?? [];
  return (
    <>
      <span className="tree-line">
        {children.length > 0 && (
          <button
            type="button"
            className="tree-caret"
            aria-expanded={expanded}
            aria-label={`${expanded ? "Collapse" : "Expand"} ${node.label}`}
            onClick={() => setExpanded((value) => !value)}
          />
        )}
        <NavLink
          className="tree-row"
          data-kind={node.kind}
          style={{ "--tree-depth": depth } as CSSProperties}
          to={node.to}
          end
        >
          <span className="tree-label">{node.label}</span>
          {node.count !== undefined && <span className="tree-count">{node.count}</span>}
        </NavLink>
      </span>
      {expanded &&
        children.map((child) => (
          <TreeRow key={child.id} node={child} depth={depth + 1} filter={filter} />
        ))}
    </>
  );
}

export function HierarchyTree({ nodes }: { nodes: TreeNode[] }) {
  const [filter, setFilter] = useState("");
  return (
    <aside className="tree-rail" data-testid="tree-rail">
      <input
        className="tree-filter"
        placeholder="Filter hierarchy"
        aria-label="Filter hierarchy"
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      <nav className="tree" aria-label="Inventory tree">
        {nodes.map((node) => (
          <TreeRow key={node.id} node={node} depth={0} filter={filter.toLowerCase()} />
        ))}
      </nav>
    </aside>
  );
}
