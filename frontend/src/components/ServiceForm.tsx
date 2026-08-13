/**
 * One deployment service's credential form (task E5.12a; spec 16.2).
 *
 * **Rendered from `SERVICE_SCHEMA`, never from a hardcoded field list.** The
 * five services have five field sets and one renderer; adding a field to the
 * Pydantic model and to the descriptor is the whole change, and
 * `tests/services-schema.test.ts` fails if only one of the two happens.
 *
 * **Secrets are write-only, and this component is where that is true or not.**
 * A stored credential arrives as the D51 keep sentinel, so there is no value
 * to populate an input with — the field renders its SET-NESS and an empty
 * replacement input, exactly as the config editor's secret rows do. Nothing
 * here reads a credential, because nothing here is ever sent one.
 *
 * **The save is wholesale per service** (`putServices`' contract): every field
 * this form owns is submitted every time, because a field omitted from the
 * body is a field the API clears.
 */
import { FormEvent, useEffect, useState } from "react";

import {
  KEEP_SENTINEL,
  Service,
  ServiceDescriptor,
  ServiceField,
  ServiceSettingsIn,
  isSecretSet,
} from "../lib/services";

/** What the operator does with a secret field between two saves. */
type SecretMode = "keep" | "replace" | "clear";

interface Draft {
  /** text / textarea / number fields, held as strings — an input's value is a
   * string and coercing on the way out keeps "" distinguishable from 0. */
  text: Record<string, string>;
  booleans: Record<string, boolean>;
  /** Whether the SERVER says this secret is set. Not the value; there is none. */
  stored: Record<string, boolean>;
  modes: Record<string, SecretMode>;
  /** Replacement plaintext, held only until save. Never rendered as a value
   * anywhere but its own password input. */
  entered: Record<string, string>;
}

function initialDraft(descriptor: ServiceDescriptor, service: Service | undefined): Draft {
  const draft: Draft = { text: {}, booleans: {}, stored: {}, modes: {}, entered: {} };
  const settings = service?.settings ?? {};
  for (const field of descriptor.fields) {
    const value = settings[field.name];
    if (field.type === "secret") {
      draft.stored[field.name] = isSecretSet(value);
      draft.modes[field.name] = "keep";
      draft.entered[field.name] = "";
    } else if (field.type === "boolean") {
      // Absent means the service has no row yet. `tls_enabled` defaults on,
      // which is the model's default and the one worth defaulting to.
      draft.booleans[field.name] = value === undefined ? true : Boolean(value);
    } else {
      draft.text[field.name] = value === undefined || value === null ? "" : String(value);
    }
  }
  return draft;
}

/**
 * The submitted body for one service. Blank optional fields are OMITTED
 * rather than sent as "" — the API reads omission as "unset", and an empty
 * string would be a stored empty URL that every tester would then fail on.
 */
function draftToSettings(descriptor: ServiceDescriptor, draft: Draft): ServiceSettingsIn {
  const out: ServiceSettingsIn = {};
  for (const field of descriptor.fields) {
    if (field.type === "secret") {
      const mode = draft.modes[field.name];
      const entered = draft.entered[field.name];
      if (mode === "replace" && entered !== "") {
        out[field.name] = entered;
      } else if (mode !== "clear" && draft.stored[field.name]) {
        // Keep what is stored. The sentinel is the only way to say that
        // without holding the credential.
        out[field.name] = KEEP_SENTINEL;
      }
      continue;
    }
    if (field.type === "boolean") {
      out[field.name] = draft.booleans[field.name];
      continue;
    }
    const raw = draft.text[field.name].trim();
    if (raw === "") {
      if (field.required) {
        // Send it anyway and let the API's 422 locate it: a required field
        // silently dropped would look like a successful save.
        out[field.name] = "";
      }
      continue;
    }
    out[field.name] = field.type === "number" ? Number(raw) : raw;
  }
  return out;
}

function SecretInput({
  field,
  draft,
  disabled,
  onChange,
  idPrefix,
}: {
  field: ServiceField;
  draft: Draft;
  disabled: boolean;
  onChange: (next: Draft) => void;
  idPrefix: string;
}) {
  const mode = draft.modes[field.name];
  const stored = draft.stored[field.name];
  const set = (patch: Partial<Draft>) => onChange({ ...draft, ...patch });
  const setMode = (next: SecretMode) =>
    set({
      modes: { ...draft.modes, [field.name]: next },
      entered: { ...draft.entered, [field.name]: "" },
    });

  // Never stored and never entered: there is nothing to keep, so the input is
  // the whole affordance and no Replace button is needed to reach it.
  const showInput = mode === "replace" || !stored;

  return (
    <div className="service-secret">
      <div className="service-secret-state">
        <span className="mono" aria-hidden="true">
          ••••••••
        </span>
        <span className="service-secret-word">
          {mode === "clear" ? "will be cleared" : stored ? "set" : "not set"}
        </span>
        {!disabled && stored && mode === "keep" && (
          <>
            <button type="button" className="btn-tertiary" onClick={() => setMode("replace")}>
              Replace
            </button>
            <button type="button" className="btn-tertiary" onClick={() => setMode("clear")}>
              Clear
            </button>
          </>
        )}
        {!disabled && stored && mode !== "keep" && (
          <button type="button" className="btn-tertiary" onClick={() => setMode("keep")}>
            Keep stored value
          </button>
        )}
      </div>
      {showInput && mode !== "clear" && (
        <input
          id={`${idPrefix}-${field.name}`}
          type="password"
          autoComplete="new-password"
          placeholder="new value — write-only"
          disabled={disabled}
          value={draft.entered[field.name]}
          onChange={(event) =>
            set({
              entered: { ...draft.entered, [field.name]: event.target.value },
              modes: { ...draft.modes, [field.name]: "replace" },
            })
          }
        />
      )}
    </div>
  );
}

function FieldRow({
  field,
  draft,
  disabled,
  onChange,
  idPrefix,
}: {
  field: ServiceField;
  draft: Draft;
  disabled: boolean;
  onChange: (next: Draft) => void;
  idPrefix: string;
}) {
  const id = `${idPrefix}-${field.name}`;
  const label = `${field.label}${field.required ? "" : " (optional)"}`;
  return (
    <div className="form-field" data-testid={`${id}-field`}>
      <label htmlFor={id}>{label}</label>
      {field.type === "secret" ? (
        <SecretInput
          field={field}
          draft={draft}
          disabled={disabled}
          onChange={onChange}
          idPrefix={idPrefix}
        />
      ) : field.type === "boolean" ? (
        <input
          id={id}
          type="checkbox"
          className="service-checkbox"
          disabled={disabled}
          checked={draft.booleans[field.name]}
          onChange={(event) =>
            onChange({
              ...draft,
              booleans: { ...draft.booleans, [field.name]: event.target.checked },
            })
          }
        />
      ) : field.type === "textarea" ? (
        <textarea
          id={id}
          rows={4}
          disabled={disabled}
          required={field.required}
          placeholder={field.placeholder}
          value={draft.text[field.name]}
          onChange={(event) =>
            onChange({ ...draft, text: { ...draft.text, [field.name]: event.target.value } })
          }
        />
      ) : (
        <input
          id={id}
          type={field.type === "number" ? "number" : "text"}
          disabled={disabled}
          required={field.required}
          placeholder={field.placeholder}
          value={draft.text[field.name]}
          onChange={(event) =>
            onChange({ ...draft, text: { ...draft.text, [field.name]: event.target.value } })
          }
        />
      )}
      {field.help && <p className="form-help">{field.help}</p>}
    </div>
  );
}

export function ServiceForm({
  descriptor,
  service,
  canManage,
  saving,
  testing,
  error,
  onSave,
  onTest,
}: {
  descriptor: ServiceDescriptor;
  service: Service | undefined;
  canManage: boolean;
  saving: boolean;
  testing: boolean;
  error: string | null;
  onSave: (settings: ServiceSettingsIn) => void;
  onTest: (settings: ServiceSettingsIn) => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => initialDraft(descriptor, service));
  const [dirty, setDirty] = useState(false);

  // Re-seed from the server only when the operator has nothing unsaved: a
  // background refetch must not take away what someone is typing. A save sets
  // `dirty` false, which is what lets the fresh row (and its new set-ness)
  // land here.
  useEffect(() => {
    if (!dirty) {
      setDraft(initialDraft(descriptor, service));
    }
  }, [descriptor, service, dirty]);

  const update = (next: Draft) => {
    setDraft(next);
    setDirty(true);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setDirty(false);
    onSave(draftToSettings(descriptor, draft));
  };

  const idPrefix = `service-${descriptor.key}`;
  return (
    <form className="form service-form" data-testid={`${idPrefix}-form`} onSubmit={submit}>
      {descriptor.fields.map((field) => (
        <FieldRow
          key={field.name}
          field={field}
          draft={draft}
          disabled={!canManage}
          onChange={update}
          idPrefix={idPrefix}
        />
      ))}
      {canManage && (
        <div className="form-actions">
          <button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={testing}
            // Tests what is IN THE FORM, saved or not: spec 16.2 validates an
            // entry before accepting it, and a candidate test writes no
            // verdict of record (the API's rule, stated here so the button's
            // meaning is not a surprise).
            onClick={() => onTest(draftToSettings(descriptor, draft))}
          >
            {testing ? "Testing…" : "Test connection"}
          </button>
        </div>
      )}
      {error && (
        <p className="form-error" data-testid={`${idPrefix}-error`}>
          {error}
        </p>
      )}
    </form>
  );
}
