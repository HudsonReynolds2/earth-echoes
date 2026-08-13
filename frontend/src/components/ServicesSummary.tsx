/**
 * The rolled-up status, and the spec 16.5 gate (task E5.12b; screen S5).
 *
 * **Two vocabularies, deliberately not aliases.** `services_status` is the
 * deployment's rollup — `unconfigured` / `pending_verification` / `verified` /
 * `degraded` — and each service carries its own `untested` / `verified` /
 * `failed`. A UI that rendered one as the other would tell an operator their
 * whole deployment is broken because one optional service is.
 *
 * **The gate is spec 16.5 and this component only REPORTS it.** Provisioning
 * bundle generation requires at minimum a verified broker, because the
 * bootstrap block embeds broker credentials; and the spec asks the UI to warn
 * when the remaining services are not verified, since devices would come
 * online with nowhere to ship analysis, metrics or audio. Enforcing it at
 * generation time is E4's, whose bundle generator does not exist yet — this
 * page shows the operator the state of the gate, it does not implement it.
 */
import { Link } from "react-router-dom";

import { SERVICE_SCHEMA, ServicesStatus, ServicesStatusValue } from "../lib/services";

const ROLLUP_TONE: Record<ServicesStatusValue, string> = {
  unconfigured: "none",
  pending_verification: "wait",
  verified: "ok",
  degraded: "bad",
};

const ROLLUP_GLYPH: Record<ServicesStatusValue, string> = {
  unconfigured: "○",
  pending_verification: "◔",
  verified: "✓",
  degraded: "✕",
};

const ROLLUP_NOTE: Record<ServicesStatusValue, string> = {
  unconfigured:
    "Nothing is configured yet. This is a deployment nobody has started, not one that is failing.",
  pending_verification:
    "Something is configured, nothing is failing, and not everything required has passed yet.",
  verified: "Every required service is configured and has passed a real connection test.",
  degraded:
    "A required service is failing. It stays this way until a test passes — never optimistically.",
};

export function ServicesSummary({ status }: { status: ServicesStatus | undefined }) {
  if (status === undefined) {
    return null;
  }
  const rollup = status.services_status;
  const rows = SERVICE_SCHEMA.map((descriptor) => status.services[descriptor.key]).filter(
    (row) => row !== undefined,
  );
  const required = rows.filter((row) => row.required);
  const verified = required.filter((row) => row.status === "verified");
  const brokerVerified = status.services.mqtt?.status === "verified";
  const unverifiedRequired = required.filter((row) => row.status !== "verified");

  return (
    <aside className="services-summary" data-testid="services-summary">
      <section className="card">
        <span className="eyebrow">Rolled-up status</span>
        <div className="services-rollup">
          <span className="services-rollup-count" data-service-status={ROLLUP_TONE[rollup]}>
            {verified.length}/{required.length}
          </span>
          <div>
            <p className="services-rollup-value" data-testid="services-rollup">
              <span aria-hidden="true">{ROLLUP_GLYPH[rollup]}</span> {rollup}
            </p>
            <p className="services-rollup-note">{ROLLUP_NOTE[rollup]}</p>
          </div>
        </div>
        <p className="services-summary-note">
          {/* NOT "re-checks run every five minutes", which is what the S5 mock
              says: periodic re-checks are closed as deliberately not built
              (D133). Timed polling reports a fact that was true minutes ago.
              Degradation comes from observed events only, and this sentence
              says which ones. */}
          Nothing re-checks these on a timer, by design. A service degrades on what is actually
          observed: a test you run here, a rotation's re-verification, and for the broker the
          control plane's own connection and last-will. {status.degrade_after_failures} consecutive
          failures demote a verified service.
        </p>
      </section>

      <section className="card" data-testid="services-gate">
        <span className="eyebrow">Provisioning</span>
        {brokerVerified ? (
          <>
            <p className="services-gate-state" data-gate="open">
              Unblocked — the broker is verified.
            </p>
            {unverifiedRequired.length > 0 && (
              <p className="services-gate-warning" data-testid="services-gate-warning">
                But {unverifiedRequired.length} required service
                {unverifiedRequired.length === 1 ? " is" : "s are"} not verified. Devices
                provisioned now come online with nowhere to ship analysis, metrics or audio until
                that is fixed.
              </p>
            )}
          </>
        ) : (
          <p className="services-gate-state" data-gate="closed" data-testid="services-gate-closed">
            Blocked — a provisioning bundle embeds the device's broker credentials, so spec 16.5
            requires a verified broker before one can be generated.
          </p>
        )}
        <Link className="btn-tertiary" to="/provisioning">
          Go to provisioning
        </Link>
      </section>
    </aside>
  );
}
