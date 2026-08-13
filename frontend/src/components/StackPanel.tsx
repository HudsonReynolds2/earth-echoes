/**
 * Path B — the generated stack (task E5.12b; spec 16.3, screen S5).
 *
 * The operator who has no services yet gets a whole stack rendered for them:
 * a compose file, five configured services and every credential minted by the
 * platform. Three actions and a warning:
 *
 * * **Generate** mints every credential and writes the five rows BEFORE a
 *   byte is rendered (fixed choice 7), and leaves every service `untested` —
 *   a generated stack does not get to vouch for itself. The operator runs it,
 *   then tests it from the cards above.
 * * **Download** re-renders deterministically from those rows. The platform
 *   keeps no blob, which is why two downloads are byte-identical.
 * * **Rotate** regenerates, re-renders, re-verifies and republishes, so a
 *   rotation is a config revision rather than a manual redistribution. It can
 *   come back `verified: false` and still have published — see the note the
 *   panel renders, which is the endpoint's own inverted ordering made visible.
 *
 * **There is no "does a stack exist" flag on any endpoint**, deliberately:
 * fixed choice 7 keeps no record of a bundle, and the download simply 404s
 * when nothing was generated. So Download and Rotate are always offered and a
 * missing stack is reported as what it is, rather than guessed at.
 */
import { FormEvent, useState } from "react";

import { Stack, StackGenerateIn, StackRotate, TestResult } from "../lib/services";

export function StackPanel({
  generating,
  downloading,
  rotating,
  stack,
  rotation,
  error,
  notice,
  onGenerate,
  onDownload,
  onRotate,
}: {
  generating: boolean;
  downloading: boolean;
  rotating: boolean;
  /** Set once this session has generated one. Its absence proves nothing —
   * a stack generated last week is still downloadable. */
  stack: Stack | null;
  rotation: StackRotate | null;
  error: string | null;
  notice: string | null;
  onGenerate: (input: StackGenerateIn) => void;
  onDownload: () => void;
  onRotate: (input: StackGenerateIn) => void;
}) {
  const [hostname, setHostname] = useState("localhost");
  const [ip, setIp] = useState("");
  const [includeObjectStorage, setIncludeObjectStorage] = useState(false);
  const [confirmRotate, setConfirmRotate] = useState(false);

  const input = (): StackGenerateIn => ({
    hostname: hostname.trim(),
    ip: ip.trim() === "" ? null : ip.trim(),
    include_object_storage: includeObjectStorage,
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    onGenerate(input());
  };

  return (
    <section className="card" data-testid="stack-panel">
      <header className="service-card-head">
        <h2>Generate a stack</h2>
        <p className="muted">
          No services yet? The platform renders a complete one — compose file, broker, database,
          metrics, dashboards — with every credential minted here and already registered against
          this deployment.
        </p>
      </header>

      <form className="form" data-testid="stack-form" onSubmit={submit}>
        <div className="form-field">
          <label htmlFor="stack-hostname">Hostname</label>
          <input
            id="stack-hostname"
            value={hostname}
            required
            onChange={(event) => setHostname(event.target.value)}
          />
          <p className="form-help">
            The address the Aggregators and the platform will dial this stack at. It goes into the
            broker certificate, so it has to be right now — a certificate for the wrong name fails
            verification everywhere.
          </p>
        </div>
        <div className="form-field">
          <label htmlFor="stack-ip">IP address (optional)</label>
          <input id="stack-ip" value={ip} onChange={(event) => setIp(event.target.value)} />
          <p className="form-help">
            Added to the certificate as well, for devices that dial by address.
          </p>
        </div>
        <div className="form-field">
          <label htmlFor="stack-object-storage">Include object storage (optional)</label>
          <input
            id="stack-object-storage"
            type="checkbox"
            className="service-checkbox"
            checked={includeObjectStorage}
            onChange={(event) => setIncludeObjectStorage(event.target.checked)}
          />
          <p className="form-help">
            Only if this deployment uploads raw audio. Left off, the deployment can still reach
            verified — object storage is required exactly when it is configured.
          </p>
        </div>

        <div className="form-actions">
          <button type="submit" disabled={generating}>
            {generating ? "Generating…" : "Generate stack"}
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={downloading}
            onClick={onDownload}
          >
            {downloading ? "Downloading…" : "Download bundle"}
          </button>
        </div>
      </form>

      <p className="services-warning" data-testid="stack-credential-warning">
        The archive contains a private key and every service password in usable form.{" "}
        <strong>Treat it as a credential</strong>: it is not kept on the server, and anyone who has
        the file has this deployment&rsquo;s services.
      </p>

      {stack && (
        <p className="services-summary-note" data-testid="stack-generated">
          Generated. Every service is back to <span className="mono">untested</span> — run the stack
          with <span className="mono">docker compose up -d</span>, then test each service above.
          Rolled up, this deployment is now <span className="mono">{stack.services_status}</span>.
        </p>
      )}

      <div className="stack-rotate">
        <span className="eyebrow">Rotation</span>
        <p className="services-summary-note">
          Rotation mints new credentials, republishes them to every Aggregator through the control
          plane, and re-verifies. The old credentials stop working immediately, so download the new
          bundle and restart the stack.
        </p>
        {!confirmRotate ? (
          <button
            type="button"
            className="btn-danger"
            onClick={() => setConfirmRotate(true)}
            data-testid="stack-rotate"
          >
            Rotate credentials
          </button>
        ) : (
          <div className="form-actions" data-testid="stack-rotate-confirm">
            <button
              type="button"
              className="btn-danger"
              disabled={rotating}
              onClick={() => {
                setConfirmRotate(false);
                onRotate(input());
              }}
            >
              {rotating ? "Rotating…" : "Yes, rotate every credential"}
            </button>
            <button type="button" className="btn-tertiary" onClick={() => setConfirmRotate(false)}>
              Cancel
            </button>
          </div>
        )}
      </div>

      {rotation && (
        <div className="services-summary-note" data-testid="stack-rotation-result">
          <p>
            Rotated. {rotation.revisions} device configuration
            {rotation.revisions === 1 ? "" : "s"} republished
            {rotation.revisions === 0 ? " (no Aggregators to tell)" : ""}.
          </p>
          {rotation.verified ? (
            <p>Re-verification passed; this deployment is verified again.</p>
          ) : (
            // The acceptance's inverted order, made visible rather than
            // hidden: the likeliest reason this fails is that the stack has
            // not been restarted with the new credentials yet, which is
            // exactly when the devices need them most.
            <p data-testid="stack-rotation-unverified">
              Re-verification did not pass, and the new credentials were published anyway. That is
              deliberate: the devices need them precisely because the old ones stopped working. Left
              at <span className="mono">{rotation.services_status}</span> — restart the stack with
              the new bundle, then test again.
            </p>
          )}
          <RotationResults results={rotation.results} />
        </div>
      )}

      {notice && (
        <p className="services-summary-note" data-testid="stack-notice">
          {notice}
        </p>
      )}
      {error && (
        <p className="form-error" data-testid="stack-error">
          {error}
        </p>
      )}
    </section>
  );
}

function RotationResults({ results }: { results: TestResult[] }) {
  if (results.length === 0) {
    return null;
  }
  return (
    <ul className="stack-rotation-outcomes">
      {results.map((result) => (
        <li key={result.service_key}>
          <span className="mono">{result.service_key}</span> — {result.outcome}
        </li>
      ))}
    </ul>
  );
}
