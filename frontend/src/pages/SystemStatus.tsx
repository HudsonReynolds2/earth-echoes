import { PageHeader } from "../components/PageHeader";
import { useHealth } from "../lib/useHealth";

export function SystemStatus() {
  const { data, isPending, isError } = useHealth();

  return (
    <div className="page">
      <PageHeader eyebrow="Platform" title="System" />
      <section className="card">
        {isPending && <p data-testid="health-loading">Checking API health...</p>}
        {isError && (
          <p className="status-bad" data-testid="health-error">
            API unreachable
          </p>
        )}
        {data && (
          <dl data-testid="health-data">
            <dt>API</dt>
            <dd className={data.status === "ok" ? "status-ok" : "status-bad"}>{data.status}</dd>
            <dt>Database</dt>
            <dd className={data.database === "ok" ? "status-ok" : "status-bad"}>{data.database}</dd>
            <dt>Build</dt>
            <dd className="mono">{data.build_sha}</dd>
          </dl>
        )}
      </section>
    </div>
  );
}
