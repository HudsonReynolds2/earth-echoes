/**
 * Listener detail (task E1.8): inventory facts only — identity, plain GPS
 * fields (the guided fill-in flow is E4.11), tags, and the destructive
 * delete. The drawer's config/telemetry/timeline content belongs to
 * E2/E3/E5/E7 and is deliberately absent (no fabricated status).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { Can } from "../../components/Can";
import { EmptyState } from "../../components/EmptyState";
import { PageHeader } from "../../components/PageHeader";
import { TagEditor } from "../../components/TagEditor";
import { deleteListener, getListener, patchListener } from "../../lib/inventory";

export function ListenerDetail() {
  const { mac = "" } = useParams();
  const decoded = decodeURIComponent(mac);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const listener = useQuery({
    queryKey: ["listener", decoded],
    queryFn: () => getListener(decoded),
  });
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const save = useMutation({
    mutationFn: () =>
      patchListener(decoded, {
        name,
        gps_lat: lat === "" ? null : Number(lat),
        gps_lon: lon === "" ? null : Number(lon),
      }),
    onSuccess: () => {
      setEditing(false);
      void queryClient.invalidateQueries();
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteListener(decoded),
    onSuccess: () => {
      void queryClient.invalidateQueries();
      navigate("/inventory");
    },
  });

  if (listener.isLoading) {
    return (
      <div data-testid="inventory-loading">
        <div className="skeleton skeleton-row" />
        <p className="skeleton-caption">Loading listener · layout holds final geometry</p>
      </div>
    );
  }
  if (listener.isError || !listener.data) {
    return (
      <EmptyState title="Listener not found" testId="listener-missing">
        It may have been deleted, or it may be outside your assigned scope.
      </EmptyState>
    );
  }

  const row = listener.data;
  return (
    <>
      <PageHeader eyebrow="Listener" title={row.name}>
        <Can permission="manage_devices" deploymentId={row.deployment_id}>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => {
              setName(row.name);
              setLat(row.gps_lat === null ? "" : String(row.gps_lat));
              setLon(row.gps_lon === null ? "" : String(row.gps_lon));
              setEditing((value) => !value);
            }}
          >
            Edit
          </button>
          <button
            type="button"
            className="btn-danger"
            data-testid="delete-listener"
            onClick={() => remove.mutate()}
          >
            Delete listener
          </button>
        </Can>
      </PageHeader>
      <p className="scope-caption">
        <span className="level-badge">Listener</span>{" "}
        <span className="mono" data-testid="listener-mac">
          {row.mac}
        </span>
      </p>
      <TagEditor entity="listeners" id={row.mac} tags={row.tags} deploymentId={row.deployment_id} />
      <section className="card" data-testid="listener-facts">
        <h2>Placement</h2>
        <dl className="import-summary">
          <dt>GPS</dt>
          <dd>
            {row.gps_lat !== null && row.gps_lon !== null
              ? `${row.gps_lat}, ${row.gps_lon}`
              : "not surveyed yet"}
          </dd>
          <dt>Registered</dt>
          <dd>{row.created_at}</dd>
        </dl>
        <p className="muted">
          Live status arrives with E3 · effective config with E2 · telemetry with E5.
        </p>
      </section>
      {editing && (
        <section className="card">
          <form
            className="form"
            data-testid="edit-listener-form"
            onSubmit={(event) => {
              event.preventDefault();
              save.mutate();
            }}
          >
            <div className="form-field">
              <label htmlFor="edit-name">Name</label>
              <input
                id="edit-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
              />
            </div>
            <div className="form-field">
              <label htmlFor="edit-lat">GPS latitude</label>
              <input id="edit-lat" value={lat} onChange={(event) => setLat(event.target.value)} />
              <p className="form-help">
                Plain fields for now — the guided fill-in flow arrives with E4.11.
              </p>
            </div>
            <div className="form-field">
              <label htmlFor="edit-lon">GPS longitude</label>
              <input id="edit-lon" value={lon} onChange={(event) => setLon(event.target.value)} />
            </div>
            <div className="form-actions">
              <button type="submit" disabled={save.isPending}>
                Save
              </button>
              <button type="button" className="btn-tertiary" onClick={() => setEditing(false)}>
                Cancel
              </button>
            </div>
            {save.isError && <p className="form-error">{(save.error as Error).message}</p>}
          </form>
        </section>
      )}
      {remove.isError && <p className="form-error">{(remove.error as Error).message}</p>}
    </>
  );
}
