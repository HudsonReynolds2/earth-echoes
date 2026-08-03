/**
 * Inventory index (task E1.8): the deployments table at organization level,
 * with the S7 dual-action first-run empty state. Sorting and paging are
 * server-driven (D7); the create form is the UsersAdmin inline-card pattern.
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
import { Link, useNavigate, useOutletContext } from "react-router-dom";

import { Can } from "../../components/Can";
import { EmptyState } from "../../components/EmptyState";
import { EntityTable } from "../../components/EntityTable";
import { PageHeader } from "../../components/PageHeader";
import {
  createDeployment,
  Deployment,
  listDeployments,
  listOrganizations,
} from "../../lib/inventory";
import { InventoryOutletContext } from "./InventoryLayout";

const columns = (() => {
  const helper = createColumnHelper<Deployment>();
  return [
    helper.accessor("name", {
      header: "Name",
      cell: (info) => (
        <Link to={`/inventory/deployments/${info.row.original.id}`}>{info.getValue()}</Link>
      ),
    }),
    helper.accessor("slug", { header: "Slug", meta: { mono: true } }),
    helper.accessor("pod_count", { header: "Pods", enableSorting: false, meta: { mono: true } }),
    helper.accessor("listener_count", {
      header: "Listeners",
      enableSorting: false,
      meta: { mono: true },
    }),
    helper.accessor("tags", {
      header: "Tags",
      enableSorting: false,
      cell: (info) => (
        <span className="tag-row">
          {info.getValue().map((tag) => (
            <span key={tag} className="tag-chip">
              {tag}
            </span>
          ))}
        </span>
      ),
    }),
    helper.accessor("created_at", { header: "Created", meta: { mono: true } }),
  ];
})();

export function OrganizationLevel() {
  const { filter } = useOutletContext<InventoryOutletContext>();
  const [sorting, setSorting] = useState<SortingState>([{ id: "name", desc: false }]);
  const [pagination, setPagination] = useState<PaginationState>({ pageIndex: 0, pageSize: 50 });
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const sort = sorting.length ? `${sorting[0].desc ? "-" : ""}${sorting[0].id}` : "name";
  const organizations = useQuery({
    queryKey: ["organizations"],
    queryFn: () => listOrganizations(),
  });
  const orgId = organizations.data?.items[0]?.id;
  const deployments = useQuery({
    queryKey: ["deployments", "list", sort, String(pagination.pageIndex), filter],
    queryFn: () =>
      listDeployments({
        sort,
        limit: pagination.pageSize,
        offset: pagination.pageIndex * pagination.pageSize,
        name: filter || undefined,
      }),
  });
  const create = useMutation({
    mutationFn: () => createDeployment({ organization_id: orgId ?? "", name: newName }),
    onSuccess: (deployment) => {
      setShowCreate(false);
      setNewName("");
      void queryClient.invalidateQueries();
      navigate(`/inventory/deployments/${deployment.id}`);
    },
  });

  const rows = deployments.data?.items ?? [];
  const total = deployments.data?.total ?? 0;
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

  if (deployments.isLoading) {
    return (
      <div data-testid="inventory-loading">
        <div className="skeleton skeleton-row" />
        <div className="skeleton skeleton-row" />
        <div className="skeleton skeleton-row" />
        <p className="skeleton-caption">Loading inventory · layout holds final geometry</p>
      </div>
    );
  }

  return (
    <>
      <PageHeader eyebrow="Organization level" title="Inventory">
        <Can permission="manage_devices">
          <Link className="btn-secondary" to="/inventory/import">
            Import inventory CSV
          </Link>
          <button type="button" onClick={() => setShowCreate((value) => !value)}>
            New deployment
          </button>
        </Can>
      </PageHeader>
      <p className="scope-caption">
        <span className="level-badge">Org level</span> {total} deployments
      </p>
      {showCreate && (
        <section className="card">
          <form
            className="form"
            data-testid="create-deployment-form"
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate();
            }}
          >
            <div className="form-field">
              <label htmlFor="new-deployment-name">Deployment name</label>
              <input
                id="new-deployment-name"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                required
              />
              <p className="form-help">
                The slug is generated from this name and keys the MQTT namespace; it locks once the
                deployment has pods.
              </p>
            </div>
            <div className="form-actions">
              <button type="submit" disabled={create.isPending}>
                Create deployment
              </button>
              <button type="button" className="btn-tertiary" onClick={() => setShowCreate(false)}>
                Cancel
              </button>
            </div>
            {create.isError && (
              <p className="form-error" data-testid="create-deployment-error">
                {(create.error as Error).message}
              </p>
            )}
          </form>
        </section>
      )}
      {total === 0 && !filter ? (
        <EmptyState title="No deployments yet" testId="inventory-first-run">
          A deployment groups pods around one telemetry stack. Create one, or bring an existing site
          in from a spreadsheet. <Link to="/inventory/import">Import inventory CSV</Link>
        </EmptyState>
      ) : (
        <EntityTable
          table={table}
          testId="deployments-table"
          caption={`${rows.length} of ${total} shown · sorted by ${sort.replace("-", "")}`}
        />
      )}
    </>
  );
}
