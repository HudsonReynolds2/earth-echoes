/**
 * Boolean switch primitive (task E2.7): role="switch" with a visible state
 * word. The track fills with ACTION INK when on — the mockup drew a green
 * track, but green is a status color and interactive-is-ink is the system
 * rule (deviation recorded in DECISIONS).
 */
export function ToggleSwitch({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className="toggle"
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className="toggle-track" aria-hidden="true">
        <span className="toggle-thumb" />
      </span>
      <span className="toggle-word">{checked ? "on" : "off"}</span>
    </button>
  );
}
