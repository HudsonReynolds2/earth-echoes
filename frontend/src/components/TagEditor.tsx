/**
 * Tag chips with an edit mode behind manage_devices (task E1.8/E1.7). PUT is
 * wholesale replace, so the editor stages the full set locally and saves
 * once. The remove glyph is "×" (U+00D7) — NOT the status "✕" (U+2715),
 * which exists only in the status-glyph subset and would render as tofu.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { putTags, TaggableEntity } from "../lib/inventory";
import { useCan } from "./Can";

export function TagEditor({
  entity,
  id,
  tags,
  deploymentId,
}: {
  entity: TaggableEntity;
  id: string;
  tags: string[];
  deploymentId: string | null;
}) {
  const canManage = useCan("manage_devices", deploymentId);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string[]>(tags);
  const [pending, setPending] = useState("");
  const queryClient = useQueryClient();
  const save = useMutation({
    mutationFn: () => putTags(entity, id, draft),
    onSuccess: () => {
      setEditing(false);
      void queryClient.invalidateQueries();
    },
  });

  if (!editing) {
    return (
      <div className="tag-row" data-testid="tag-editor">
        {tags.length === 0 && <span className="muted">No tags</span>}
        {tags.map((tag) => (
          <span key={tag} className="tag-chip">
            {tag}
          </span>
        ))}
        {canManage && (
          <button
            type="button"
            className="btn-tertiary"
            onClick={() => {
              setDraft(tags);
              setEditing(true);
            }}
          >
            Edit tags
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="tag-row" data-testid="tag-editor-editing">
      {draft.map((tag) => (
        <span key={tag} className="tag-chip">
          {tag}
          <button
            type="button"
            className="btn-tertiary"
            aria-label={`Remove tag ${tag}`}
            onClick={() => setDraft(draft.filter((existing) => existing !== tag))}
          >
            {"×"}
          </button>
        </span>
      ))}
      <input
        className="tree-filter"
        aria-label="Add tag"
        placeholder="Add tag"
        value={pending}
        onChange={(event) => setPending(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && pending.trim()) {
            event.preventDefault();
            if (!draft.includes(pending.trim())) {
              setDraft([...draft, pending.trim()]);
            }
            setPending("");
          }
        }}
      />
      <button type="button" onClick={() => save.mutate()} disabled={save.isPending}>
        Save tags
      </button>
      <button type="button" className="btn-tertiary" onClick={() => setEditing(false)}>
        Cancel
      </button>
      {save.isError && <span className="form-error">{(save.error as Error).message}</span>}
    </div>
  );
}
