/**
 * Deployment level (task E1.8): the pods table (v2's slimmed column set — the
 * six-column v1 table stays dead), the create-pod form with the E1.3 inline
 * aggregator option, tags, and the guarded delete surfacing the 409 blockers
 * verbatim.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createColumnHelper,
  getCoreRowModel,
  PaginationState,
  SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router-dom";

import { Can } from "../../components/Can";
import { EmptyState } from "../../components/EmptyState";
import { EntityTable } from "../../components/EntityTable";
import { PageHeader } from "../../components/PageHeader";
import { TagEditor } from "../../components/TagEditor";
import { createPod, deleteDeployment, getDeployment, listPods, Pod } from "../../lib/inventory";
import { InventoryOutletContext } from "./InventoryLayout";

const columns = (() => {
  const helper = createColumnHelper<Pod>();
  return [
    helper.accessor("name", {
      header: "Name",
      cell: (info) => <Link to={`/inventory/pods/${info.row.original.id}`}>{info.getValue()}</Link>,
    }),
    helper.accessor((row) => row.aggregator?.aggregator_uuid ?? "—", {
      id: "aggregator",
      header: "Aggregator",
      enableSorting: false,
      meta: { mono: true },
    }),
    helper.accessor("listener_count", {
      header: "Listeners",
      enableSorting: false,
      meta: { mono: true },
    }),
    helper.accessor("created_at", { header: "Created", meta: { mono: true } }),
  ];
})();

export function DeploymentLevel() {
  const { deploymentId = "" } = useParams();
  const { filter } = useOutletContext<InventoryOutletContext>();
  const [sorting, setSorting] = useState<SortingState>([{ id: "name", desc: false }]);
  const [pagination, setPagination] = useState<PaginationState>({ pageIndex: 0, pageSize: 50 });
  const [showCreate, setShowCreate] = useState(false);
  const [podName, setPodName] = useState("");
  const [aggUuid, setAggUuid] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const deployment = useQuery({
    queryKey: ["deployment", deploymentId],
    queryFn: () => getDeployment(deploymentId),
  });
  const sort = sorting.length ? `${sorting[0].desc ? "-" : ""}${sorting[0].id}` : "name";
  const pods = useQuery({
    queryKey: ["pods", deploymentId, sort, String(pagination.pageIndex), filter],
    queryFn: () =>
      listPods({
        deployment_id: deploymentId,
        sort,
        limit: pagination.pageSize,
        offset: pagination.pageIndex * pagination.pageSize,
        name: filter || undefined,
      }),
  });
  const create = useMutation({
    mutationFn: () =>
      createPod({
        deployment_id: deploymentId,
        name: podName,
        aggregator: aggUuid ? { aggregator_uuid: aggUuid } : {},
      }),
    onSuccess: () => {
      setShowCreate(false);
      setPodName("");
      setAggUuid("");
      void queryClient.invalidateQueries();
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteDeployment(deploymentId),
    onSuccess: () => {
      void queryClient.invalidateQueries();
      navigate("/inventory");
    },
  });

  const rows = pods.data?.items ?? [];
  const total = pods.data?.total ?? 0;
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting, pagination },
    onSortingChange: setSorting,
    onPaginationChange: setPagination,
    manualSorting: true,
    manualPagination: true,
    rowCount: total,
    getCoreRowModel: getCoreRowModel(),
  });

  if (deployment.isLoading) {
    return (
      <div data-testid="inventory-loading">
        <div className="skeleton skeleton-row" />
        <div className="skeleton skeleton-row" />
        <p className="skeleton-caption">Loading deployment · layout holds final geometry</p>
      </div>
    );
  }
  if (deployment.isError || !deployment.data) {
    return (
      <EmptyState title="Deployment not found" testId="deployment-missing">
        It may have been deleted, or it may be outside your assigned scope.
      </EmptyState>
    );
  }

  const row = deployment.data;
  return (
    <>
      <PageHeader eyebrow="Deployment level" title={row.name}>
        <Can permission="manage_devices" deploymentId={deploymentId}>
          <button type="button" onClick={() => setShowCreate((value) => !value)}>
            New pod
          </button>
          <button
            type="button"
            className="btn-danger"
            data-testid="delete-deployment"
            onClick={() => remove.mutate()}
          >
            Delete deployment
          </button>
        </Can>
      </PageHeader>
      <p className="scope-caption">
        <span className="level-badge">Deployment level</span> slug{" "}
        <span className="mono">{row.slug}</span> · {row.pod_count} pods · {row.listener_count}{" "}
        listeners inherit from here
      </p>
      <TagEditor entity="deployments" id={row.id} tags={row.tags} deploymentId={row.id} />
      {remove.isError && (
        <p className="form-error" data-testid="delete-deployment-error">
          {(remove.error as Error).message}
        </p>
      )}
      {showCreate && (
        <section className="card">
          <form
            className="form"
            data-testid="create-pod-form"
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate();
            }}
          >
            <div className="form-field">
              <label htmlFor="new-pod-name">Pod name</label>
              <input
                id="new-pod-name"
                value={podName}
                onChange={(event) => setPodName(event.target.value)}
                required
              />
            </div>
            <div className="form-field">
              <label htmlFor="new-pod-agg">Aggregator UUID (optional)</label>
              <input
                id="new-pod-agg"
                value={aggUuid}
                onChange={(event) => setAggUuid(event.target.value)}
              />
              <p className="form-help">
                Leave blank and the platform assigns one. The pod and its single aggregator are
                created together in one call.
              </p>
            </div>
            <div className="form-actions">
              <button type="submit" disabled={create.isPending}>
                Create pod
              </button>
              <button type="button" className="btn-tertiary" onClick={() => setShowCreate(false)}>
                Cancel
              </button>
            </div>
            {create.isError && (
              <p className="form-error" data-testid="create-pod-error">
                {(create.error as Error).message}
              </p>
            )}
          </form>
        </section>
      )}
      {total === 0 ? (
        <EmptyState title="No pods yet" testId="pods-empty">
          A pod is one arm of the star around a HaLow router: one aggregator plus its listeners.
        </EmptyState>
      ) : (
        <EntityTable
          table={table}
          testId="pods-table"
          caption={`${rows.length} of ${total} shown · sorted by ${sort.replace("-", "")}`}
        />
      )}
    </>
  );
}
