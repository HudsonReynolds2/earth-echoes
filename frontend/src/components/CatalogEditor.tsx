/**
 * The "a new key ships no frontend change" machine (task E2.7; spec 5.3
 * acceptance): every editor renders from the catalog row alone — type, enum
 * values, range, unit suffix. Client validation stays SOFT (number coercion
 * only); the server's 422 is authoritative. Unknown value types fall back
 * to the JSON textarea, which is also the v1 editor for capture.schedule
 * (a purpose-built schedule editor is deferred, recorded).
 */
import { useId, useState } from "react";

import { CatalogKey, unitOf } from "../lib/config";
import { ToggleSwitch } from "./ToggleSwitch";

export function CatalogEditor({
  def,
  value,
  disabled,
  onChange,
}: {
  def: CatalogKey;
  value: unknown;
  disabled: boolean;
  onChange: (next: unknown) => void;
}) {
  const inputId = useId();
  const label = `Value for ${def.key}`;
  const unit = unitOf(def.key);

  if (def.value_type === "bool") {
    return (
      <ToggleSwitch
        checked={value === true}
        onChange={onChange}
        label={label}
        disabled={disabled}
      />
    );
  }

  if (def.enum_values !== null) {
    return (
      <select
        id={inputId}
        aria-label={label}
        value={String(value ?? "")}
        disabled={disabled}
        onChange={(event) => {
          const raw = event.target.value;
          onChange(def.value_type === "int" || def.value_type === "float" ? Number(raw) : raw);
        }}
      >
        {def.enum_values.map((option) => (
          <option key={String(option)} value={String(option)}>
            {String(option)}
          </option>
        ))}
      </select>
    );
  }

  if (def.value_type === "int" || def.value_type === "float") {
    return (
      <span className="editor-with-unit">
        <input
          id={inputId}
          type="number"
          aria-label={label}
          value={value === null || value === undefined ? "" : String(value)}
          step={def.value_type === "int" ? 1 : "any"}
          min={def.min_value ?? undefined}
          max={def.max_value ?? undefined}
          disabled={disabled}
          onChange={(event) => {
            const raw = event.target.value;
            onChange(raw === "" ? null : Number(raw));
          }}
        />
        {unit && <span className="editor-unit mono">{unit}</span>}
      </span>
    );
  }

  if (def.value_type === "string") {
    return (
      <input
        id={inputId}
        type="text"
        aria-label={label}
        value={value === null || value === undefined ? "" : String(value)}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }

  // "object" (capture.schedule) and any future type: raw JSON, parsed
  // client-side for shape only; semantics are the server's.
  return <JsonEditor value={value} label={label} disabled={disabled} onChange={onChange} />;
}

function JsonEditor({
  value,
  label,
  disabled,
  onChange,
}: {
  value: unknown;
  label: string;
  disabled: boolean;
  onChange: (next: unknown) => void;
}) {
  const [text, setText] = useState(() => (value == null ? "" : JSON.stringify(value)));
  const [invalid, setInvalid] = useState(false);
  return (
    <span className="editor-json">
      <textarea
        aria-label={label}
        className="mono"
        rows={2}
        value={text}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        onChange={(event) => {
          const raw = event.target.value;
          setText(raw);
          if (raw.trim() === "") {
            setInvalid(false);
            return;
          }
          try {
            onChange(JSON.parse(raw));
            setInvalid(false);
          } catch {
            setInvalid(true); // stage nothing; the text is not yet JSON
          }
        }}
      />
      {invalid && <span className="editor-json-note">not valid JSON yet</span>}
    </span>
  );
}
