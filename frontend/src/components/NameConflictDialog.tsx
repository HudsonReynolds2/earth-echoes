/**
 * The E1.4 auto-suffix prompt (task E1.8): a 409 name conflict surfaces the
 * server's suggestion and the retry happens ONLY on the explicit click —
 * never silently (spec 4.3 item 1). Controlled overlay div, not <dialog>
 * (jsdom support is unreliable).
 */
export function NameConflictDialog({
  open,
  requestedName,
  suggestedName,
  scopeLabel,
  onUseSuffix,
  onEditName,
}: {
  open: boolean;
  requestedName: string;
  suggestedName: string;
  scopeLabel: string;
  onUseSuffix: () => void;
  onEditName: () => void;
}) {
  if (!open) {
    return null;
  }
  return (
    <div className="modal-overlay" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-label="Name already exists">
        <h2>Name already exists</h2>
        <p>
          <span className="mono">{requestedName}</span> is taken in {scopeLabel}. Create it as{" "}
          <span className="mono">{suggestedName}</span> instead?
        </p>
        <div className="form-actions">
          <button type="button" onClick={onUseSuffix} data-testid="use-suggested-name">
            Use suggested name
          </button>
          <button type="button" className="btn-secondary" onClick={onEditName}>
            Edit name
          </button>
        </div>
      </div>
    </div>
  );
}
