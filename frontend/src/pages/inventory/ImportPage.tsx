/**
 * Bulk import (task E1.8 over E1.6): setup → results → done. The first
 * submit is all-or-nothing and doubles as the dry run; committing partial
 * results is structurally impossible before the row report is on screen, and
 * requires the explicit checkbox (S4's preview-before-commit principle). Row
 * outcomes are colored words, NOT device states — the closed six-state
 * vocabulary cannot express valid/invalid, and no seventh glyph exists.
 */
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { Can } from "../../components/Can";
import { EmptyState } from "../../components/EmptyState";
import { PageHeader } from "../../components/PageHeader";
import { importAggregators, importListeners, ImportReport } from "../../lib/inventory";

type Entity = "listeners" | "aggregators";

export function ImportPage() {
  const [entity, setEntity] = useState<Entity>("listeners");
  const [format, setFormat] = useState<"csv" | "json">("csv");
  const [content, setContent] = useState("");
  const [autoSuffix, setAutoSuffix] = useState(false);
  const [report, setReport] = useState<ImportReport | null>(null);
  const [acceptPartial, setAcceptPartial] = useState(false);

  const run = useMutation({
    mutationFn: (partial: boolean) =>
      entity === "listeners"
        ? importListeners({ format, content, partial, autoSuffix })
        : importAggregators({ format, content, partial }),
    onSuccess: (result) => {
      setReport(result);
      setAcceptPartial(false);
    },
  });

  return (
    <>
      <PageHeader eyebrow="Inventory" title="Bulk import" />
      <Can
        permission="manage_devices"
        fallback={
          <EmptyState title="Bulk import needs device management access" testId="import-denied">
            Importing creates inventory rows; ask an owner or your deployment operator.
          </EmptyState>
        }
      >
        <section className="card">
          <form
            className="form"
            data-testid="import-form"
            onSubmit={(event) => {
              event.preventDefault();
              run.mutate(false); // first submit is always the all-or-nothing dry run
            }}
          >
            <div className="form-field">
              <label htmlFor="import-entity">Entity</label>
              <select
                id="import-entity"
                value={entity}
                onChange={(event) => setEntity(event.target.value as Entity)}
              >
                <option value="listeners">Listeners</option>
                <option value="aggregators">Aggregators</option>
              </select>
            </div>
            <div className="form-field">
              <label htmlFor="import-format">Format</label>
              <select
                id="import-format"
                value={format}
                onChange={(event) => setFormat(event.target.value as "csv" | "json")}
              >
                <option value="csv">CSV</option>
                <option value="json">JSON rows</option>
              </select>
              <p className="form-help">
                CSV columns are fixed — see guide/bulk-import.md. JSON is a bare array of row
                objects.
              </p>
            </div>
            <div className="form-field">
              <label htmlFor="import-content">File contents</label>
              <textarea
                id="import-content"
                rows={8}
                value={content}
                onChange={(event) => setContent(event.target.value)}
                required
              />
            </div>
            {entity === "listeners" && (
              <div className="form-field">
                <label>
                  <input
                    type="checkbox"
                    checked={autoSuffix}
                    onChange={(event) => setAutoSuffix(event.target.checked)}
                  />{" "}
                  Auto-suffix colliding names (explicit; never silent)
                </label>
              </div>
            )}
            <div className="form-actions">
              <button type="submit" disabled={run.isPending}>
                Validate &amp; import
              </button>
            </div>
            {run.isError && (
              <p className="form-error" data-testid="import-error">
                {(run.error as Error).message}
              </p>
            )}
          </form>
        </section>
        {report && (
          <section className="card" data-testid="import-results">
            <h2>{report.committed ? "Imported" : "Nothing imported — review the rows"}</h2>
            <dl className="import-summary">
              <dt>Total</dt>
              <dd>{report.rows.length}</dd>
              <dt>Created</dt>
              <dd>{report.committed ? report.created : 0}</dd>
              <dt>Failed</dt>
              <dd className="outcome-error">{report.failed}</dd>
            </dl>
            <div className="data-table-wrap">
              <table className="data-table" data-testid="import-rows">
                <thead>
                  <tr>
                    <th>Row</th>
                    <th>Name</th>
                    <th>Identifier</th>
                    <th>Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {report.rows.map((row) => (
                    <tr
                      key={row.row}
                      className={row.status === "error" ? "row-invalid" : undefined}
                    >
                      <td className="cell-mono">{row.row}</td>
                      <td>{row.name ?? "—"}</td>
                      <td className="cell-mono">{row.entity_id ?? "—"}</td>
                      <td>
                        {row.status === "created" ? (
                          <span className="outcome-created">
                            {report.committed ? "created" : "valid"}
                          </span>
                        ) : (
                          <span className="outcome-error">
                            {row.error?.code}: {row.error?.message}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!report.committed && report.failed > 0 && (
              <div className="form-field">
                <label>
                  <input
                    type="checkbox"
                    data-testid="accept-partial"
                    checked={acceptPartial}
                    onChange={(event) => setAcceptPartial(event.target.checked)}
                  />{" "}
                  Import the {report.rows.length - report.failed} valid rows and skip{" "}
                  {report.failed} failed rows
                </label>
                <div className="form-actions">
                  <button
                    type="button"
                    data-testid="commit-partial"
                    disabled={!acceptPartial || run.isPending}
                    onClick={() => run.mutate(true)}
                  >
                    Import {report.rows.length - report.failed} rows
                  </button>
                </div>
              </div>
            )}
            {report.committed && (
              <p>
                <Link to="/inventory">View inventory</Link>
              </p>
            )}
          </section>
        )}
      </Can>
    </>
  );
}
