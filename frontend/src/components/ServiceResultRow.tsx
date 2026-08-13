/**
 * One service's verdict, and what to do about it (task E5.12a; spec 16.3, S5).
 *
 * **Its own status vocabulary, deliberately not `StatusChip`.** A device's
 * spec 9.3 status and a service connection's status are different facts with
 * different words, and rendering one through the other's component is exactly
 * the conflation D40 exists to prevent. Same hues — green still reads as good
 * — different glyphs, different labels, different component.
 *
 * **Every failing check shows its remedy.** E5.3 makes `remedy` non-empty on
 * every failed check and the suite asserts it, because S5's premise is that
 * the person reading this failure is the person who can fix the service. A
 * failure rendered without its remedy would waste that.
 *
 * Each row carries its own retry: one service failing never blocks reading or
 * re-testing the other four.
 */
import {
  Service,
  ServiceDescriptor,
  ServiceStatus,
  ServiceStatusValue,
  TestResult,
} from "../lib/services";

const STATUS_LABEL: Record<ServiceStatusValue, string> = {
  untested: "untested",
  verified: "verified",
  failed: "failed",
};

/** Color + glyph + word, the house rule, in this vocabulary's own terms. */
const STATUS_TONE: Record<ServiceStatusValue, string> = {
  untested: "wait",
  verified: "ok",
  failed: "bad",
};

const STATUS_GLYPH: Record<ServiceStatusValue, string> = {
  untested: "◔",
  verified: "✓",
  failed: "✕",
};

export function ServiceChip({ status }: { status: ServiceStatusValue }) {
  return (
    <span className="service-chip" data-service-status={STATUS_TONE[status]}>
      <span aria-hidden="true">{STATUS_GLYPH[status]}</span>
      {STATUS_LABEL[status]}
    </span>
  );
}

function tested(at: string | null): string {
  return at === null ? "never tested" : `last tested ${new Date(at).toLocaleString()}`;
}

/**
 * The outcomes that are NOT failures, spelled out. `not_required` and
 * `not_configured` are verdicts about whether the question applies, and an
 * operator who sees them rendered as red has been told something false
 * (E5.3's four outcomes, two of which are not failures).
 */
const OUTCOME_NOTE: Record<string, string> = {
  pass: "Every check passed.",
  fail: "At least one check failed.",
  not_required: "Not required for this deployment — no credentials entered, nothing to test.",
  not_configured: "Not configured yet. Enter the credentials above and test.",
};

export function ServiceResultRow({
  descriptor,
  service,
  status,
  result,
  canManage,
  testing,
  onRetest,
}: {
  descriptor: ServiceDescriptor;
  service: Service | undefined;
  status: ServiceStatus | undefined;
  /** This session's test output, if this service has been tested since load. */
  result: TestResult | undefined;
  canManage: boolean;
  testing: boolean;
  onRetest: () => void;
}) {
  const value: ServiceStatusValue = service?.status ?? "untested";
  const required = status?.required ?? true;
  const failures = result?.checks.filter((check) => !check.passed) ?? [];

  return (
    <div className="service-result" data-testid={`service-${descriptor.key}-result`}>
      <div className="service-result-head">
        <ServiceChip status={value} />
        {!required && (
          <span className="service-tag" data-testid={`service-${descriptor.key}-optional`}>
            optional
          </span>
        )}
        <span className="service-result-time mono">{tested(service?.last_tested_at ?? null)}</span>
        {canManage && (
          <button
            type="button"
            className="btn-secondary service-result-retry"
            disabled={testing}
            onClick={onRetest}
          >
            {testing ? "Testing…" : "Re-test"}
          </button>
        )}
      </div>

      {service?.status_reason && <p className="service-result-reason">{service.status_reason}</p>}

      {service !== undefined && service.consecutive_failures > 0 && (
        <p className="service-result-reason muted">
          {service.consecutive_failures} consecutive failure
          {service.consecutive_failures === 1 ? "" : "s"}.
        </p>
      )}

      {result && (
        <div className="service-checks" data-testid={`service-${descriptor.key}-checks`}>
          <p className="service-result-reason">{OUTCOME_NOTE[result.outcome] ?? result.outcome}</p>
          {result.checks.map((check) => (
            <div
              key={check.name}
              className="service-check"
              data-passed={check.passed ? "yes" : "no"}
            >
              <span className="service-check-name">
                <span aria-hidden="true">{check.passed ? "✓" : "✕"}</span> {check.name}
              </span>
              <span className="service-check-detail">{check.detail}</span>
              <span className="service-check-time mono">{check.elapsed_ms}ms</span>
            </div>
          ))}
          {failures.length > 0 && (
            <div className="service-remedies">
              <span className="eyebrow">How to fix it</span>
              {failures.map((check) => (
                <p key={check.name} className="service-remedy">
                  <span className="service-remedy-name">{check.name}</span> {check.remedy}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
