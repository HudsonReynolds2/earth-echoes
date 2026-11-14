/**
 * The bundle generation wizard (task E4.10; spec 8.2, 8.3).
 *
 * Four steps, in the order an operator decides things: what to provision (one
 * Pod or the whole Deployment), how each Pod's cards are written (spec 8.2's
 * modes, chosen per Pod), a review of exactly what will come out, then
 * generate. S6 draws only the board; the wizard is assembled from the
 * product's existing form vocabulary plus `.wizard-steps` (D180).
 *
 * What the wizard refuses up front is only what the API would refuse on shape:
 * a mixed Pod needs at least one Listener named, and a Pod without an
 * Aggregator has nothing to provision against. Configuration problems
 * (network values unset, services not verified) are the API's to judge, and
 * the wizard explains its 422s in terms of what to go and fix.
 */
import { useQuery } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";

import { Pod, listListeners } from "../lib/inventory";
import {
  ApiError,
  BUNDLE_MODES,
  BundleDetail,
  BundleMode,
  CreateBundleIn,
  GenerationRefusal,
  createBundle,
} from "../lib/provisioning";

const STEPS = ["Target", "Card modes", "Review", "Generate"] as const;

const MODE_LABELS: Record<BundleMode, { label: string; help: string }> = {
  per_pod: {
    label: "One file for the whole Pod",
    help: "The same card for every Listener. Names and GPS are filled in later from the cloud.",
  },
  per_listener: {
    label: "One file per Listener",
    help: "Each card carries its own Listener's name and settings.",
  },
  mixed: {
    label: "Pod file plus a few per-Listener files",
    help: "The Pod file for most cards; named Listeners get their own.",
  },
};

interface Choice {
  mode: BundleMode;
  macs: string[];
}

export function BundleWizard({
  deploymentId,
  deploymentName,
  pods,
  onGenerated,
  onCancel,
}: {
  deploymentId: string;
  deploymentName: string;
  pods: Pod[];
  onGenerated: (bundle: BundleDetail) => void;
  onCancel: () => void;
}) {
  const provisionable = useMemo(() => pods.filter((pod) => pod.aggregator !== null), [pods]);
  const [step, setStep] = useState(0);
  const [scope, setScope] = useState<"pod" | "deployment">("pod");
  const [podId, setPodId] = useState(provisionable[0]?.id ?? "");
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; refusal: GenerationRefusal } | null>(null);

  const inScope = scope === "pod" ? provisionable.filter((p) => p.id === podId) : provisionable;
  const choiceFor = (id: string): Choice => choices[id] ?? { mode: "per_pod", macs: [] };
  const setChoice = (id: string, next: Choice) =>
    setChoices((current) => ({ ...current, [id]: next }));

  const incomplete = inScope.filter((p) => {
    const choice = choiceFor(p.id);
    return choice.mode === "mixed" && choice.macs.length === 0;
  });

  async function generate(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const input: CreateBundleIn = {
      deployment_id: deploymentId,
      scope,
      pods: inScope.map((pod) => {
        const choice = choiceFor(pod.id);
        return {
          pod_id: pod.id,
          mode: choice.mode,
          listener_macs: choice.mode === "mixed" ? choice.macs : [],
        };
      }),
    };
    try {
      onGenerated(await createBundle(input));
    } catch (caught) {
      const refusal =
        caught instanceof ApiError && caught.detail && typeof caught.detail === "object"
          ? (caught.detail as GenerationRefusal)
          : {};
      setError({ message: caught instanceof Error ? caught.message : String(caught), refusal });
    } finally {
      setBusy(false);
    }
  }

  if (provisionable.length === 0) {
    return (
      <section className="card bundle-wizard" data-testid="bundle-wizard">
        <p className="muted">
          No Pod in {deploymentName} has an Aggregator yet, so there is nothing to provision. Add
          one under Inventory first.
        </p>
        <div className="form-actions">
          <button type="button" className="btn-tertiary" onClick={onCancel}>
            Close
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="card bundle-wizard" data-testid="bundle-wizard" aria-label="New bundle">
      <ol className="wizard-steps">
        {STEPS.map((label, index) => (
          <li
            key={label}
            aria-current={index === step ? "step" : undefined}
            data-done={index < step ? "true" : undefined}
          >
            <span className="wizard-step-index">{index + 1}</span>
            {label}
          </li>
        ))}
      </ol>

      {step === 0 && (
        <div className="form">
          <fieldset className="wizard-choice">
            <legend>What are you provisioning?</legend>
            <label>
              <input
                type="radio"
                name="scope"
                checked={scope === "pod"}
                onChange={() => setScope("pod")}
              />
              One Pod
            </label>
            <label>
              <input
                type="radio"
                name="scope"
                checked={scope === "deployment"}
                onChange={() => setScope("deployment")}
              />
              Every Pod in {deploymentName} ({provisionable.length})
            </label>
          </fieldset>
          {scope === "pod" && (
            <div className="form-field">
              <label htmlFor="wizard-pod">Pod</label>
              <select id="wizard-pod" value={podId} onChange={(e) => setPodId(e.target.value)}>
                {provisionable.map((pod) => (
                  <option key={pod.id} value={pod.id}>
                    {pod.name} ({pod.listener_count} Listeners)
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
      )}

      {step === 1 && (
        <div className="form">
          {inScope.map((pod) => (
            <PodModePicker
              key={pod.id}
              pod={pod}
              choice={choiceFor(pod.id)}
              onChange={(next) => setChoice(pod.id, next)}
            />
          ))}
        </div>
      )}

      {step === 2 && (
        <div data-testid="wizard-review">
          <table className="data-table">
            <thead>
              <tr>
                <th>Pod</th>
                <th>Cards</th>
                <th>How</th>
              </tr>
            </thead>
            <tbody>
              {inScope.map((pod) => {
                const choice = choiceFor(pod.id);
                return (
                  <tr key={pod.id}>
                    <td>{pod.name}</td>
                    <td>{pod.listener_count + 1}</td>
                    <td>
                      {MODE_LABELS[choice.mode].label}
                      {choice.mode === "mixed" && ` (${choice.macs.length} named)`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="muted">
            Each Pod also gets its Aggregator&apos;s <code>settings.yaml</code>, which carries the
            Aggregator&apos;s broker login <strong>in plaintext, by design</strong> (spec 16.4).
            Treat the downloaded archive like a password.
          </p>
        </div>
      )}

      {step === 3 && (
        <form className="form" onSubmit={generate}>
          <p>
            Generate a bundle for{" "}
            {scope === "pod" ? (inScope[0]?.name ?? "this Pod") : `every Pod in ${deploymentName}`}?
          </p>
          {error && <GenerationError error={error} />}
          <div className="form-actions">
            <button type="submit" disabled={busy}>
              {busy ? "Generating…" : "Generate bundle"}
            </button>
          </div>
        </form>
      )}

      <div className="form-actions wizard-nav">
        <button type="button" className="btn-tertiary" onClick={onCancel}>
          Cancel
        </button>
        {step > 0 && (
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setStep(step - 1)}
            disabled={busy}
          >
            Back
          </button>
        )}
        {step < STEPS.length - 1 && (
          <button
            type="button"
            onClick={() => setStep(step + 1)}
            disabled={(step === 0 && inScope.length === 0) || (step === 1 && incomplete.length > 0)}
          >
            Next
          </button>
        )}
      </div>
      {step === 1 && incomplete.length > 0 && (
        <p className="form-error">
          Name at least one Listener for {incomplete.map((p) => p.name).join(", ")}, or choose a
          different mode.
        </p>
      )}
    </section>
  );
}

function PodModePicker({
  pod,
  choice,
  onChange,
}: {
  pod: Pod;
  choice: Choice;
  onChange: (next: Choice) => void;
}) {
  const aggregatorId = pod.aggregator?.id ?? "";
  const listeners = useQuery({
    queryKey: ["listeners", "aggregator", aggregatorId],
    queryFn: () => listListeners({ aggregator_id: aggregatorId, limit: 500 }),
    enabled: choice.mode === "mixed" && aggregatorId !== "",
  });
  const toggle = (mac: string) =>
    onChange({
      ...choice,
      macs: choice.macs.includes(mac)
        ? choice.macs.filter((m) => m !== mac)
        : [...choice.macs, mac],
    });

  return (
    <fieldset className="wizard-choice" data-testid={`mode-${pod.id}`}>
      <legend>{pod.name}</legend>
      {BUNDLE_MODES.map((mode) => (
        <label key={mode}>
          <input
            type="radio"
            name={`mode-${pod.id}`}
            checked={choice.mode === mode}
            onChange={() => onChange({ mode, macs: mode === "mixed" ? choice.macs : [] })}
          />
          <span>
            {MODE_LABELS[mode].label}
            <span className="form-help">{MODE_LABELS[mode].help}</span>
          </span>
        </label>
      ))}
      {choice.mode === "mixed" && (
        <div className="wizard-listeners" aria-label={`Listeners in ${pod.name}`}>
          {listeners.isLoading && <p className="muted">Loading Listeners…</p>}
          {listeners.data?.items.map((listener) => (
            <label key={listener.mac}>
              <input
                type="checkbox"
                checked={choice.macs.includes(listener.mac)}
                onChange={() => toggle(listener.mac)}
              />
              {listener.name} <span className="mono">{listener.mac}</span>
            </label>
          ))}
        </div>
      )}
    </fieldset>
  );
}

function GenerationError({ error }: { error: { message: string; refusal: GenerationRefusal } }) {
  const { missing, deployment } = error.refusal;
  return (
    <div className="form-error" role="alert" data-testid="generation-error">
      {missing && missing.length > 0 ? (
        <>
          <p>This Pod cannot join its network yet. Set these in the Pod&apos;s Configuration:</p>
          <ul>
            {missing.map((key) => (
              <li key={key} className="mono">
                {key}
              </li>
            ))}
          </ul>
        </>
      ) : deployment ? (
        <p>
          {deployment}&apos;s services are not verified yet (spec 16.5). Configure and verify the
          broker on the deployment&apos;s Services page, then generate again.
        </p>
      ) : (
        <p>{error.message}</p>
      )}
    </div>
  );
}
