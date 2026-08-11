/**
 * Pod level (task E1.8): the aggregator identity card (no separate aggregator
 * route — one per pod, E1.3), the listeners table with mono identifier
 * columns, and the create-listener form carrying the E1.4 conflict dialog:
 * the auto-suffix retry happens only on the explicit click, never silently.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createColumnHelper,
  getCoreRowModel,
  PaginationState,
  SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { Link, useOutletContext, useParams } from "react-router-dom";

import { BulkEditModal } from "../../components/BulkEditModal";
import { Can, useCan } from "../../components/Can";
import { DeviceTimeline } from "../../components/DeviceTimeline";
import { EmptyState } from "../../components/EmptyState";
import { EntityTable } from "../../components/EntityTable";
import { NameConflictDialog } from "../../components/NameConflictDialog";
import { PageHeader } from "../../components/PageHeader";
import { TagEditor } from "../../components/TagEditor";
import { getCatalog } from "../../lib/config";
import { ApiError, createListener, getPod, Listener, listListeners } from "../../lib/inventory";
import { InventoryOutletContext } from "./InventoryLayout";

/** The E2.8 selection column (spec 5.2's simple path) leads; the rest is
 * the E1.8 table unchanged. Checkbox state lives in the page, not the
 * table — a leading display column, never a data column. */
function buildColumns(
  selected: Set<string>,
  visible: Listener[],
  toggle: (mac: string) => void,
  toggleAll: () => void,
  canBulkEdit: boolean,
) {
  const helper = createColumnHelper<Listener>();
  const base = [
    helper.accessor("name", {
      header: "Name",
      cell: (info) => (
        <Link to={`/inventory/listeners/${encodeURIComponent(info.row.original.mac)}`}>
          {info.getValue()}
        </Link>
      ),
    }),
    helper.accessor("mac", { header: "MAC", meta: { mono: true } }),
    helper.accessor(
      (row) =>
        row.gps_lat !== null && row.gps_lon !== null ? `${row.gps_lat}, ${row.gps_lon}` : "—",
      { id: "gps", header: "GPS", enableSorting: false, meta: { mono: true } },
    ),
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
  if (!canBulkEdit) {
    return base;
  }
  return [
    helper.display({
      id: "select",
      header: () => (
        <input
          type="checkbox"
          aria-label="Select all listeners on this page"
          checked={visible.length > 0 && visible.every((row) => selected.has(row.mac))}
          onChange={toggleAll}
        />
      ),
      cell: (info) => (
        <input
          type="checkbox"
          aria-label={`Select ${info.row.original.name}`}
          checked={selected.has(info.row.original.mac)}
          onChange={() => toggle(info.row.original.mac)}
        />
      ),
      enableSorting: false,
      meta: { selection: true },
    }),
    ...base,
  ];
}

interface ConflictState {
  requestedName: string;
  suggestion: string;
}

export function PodLevel() {
  const { podId = "" } = useParams();
  const { filter } = useOutletContext<InventoryOutletContext>();
  const [sorting, setSorting] = useState<SortingState>([{ id: "name", desc: false }]);
  const [pagination, setPagination] = useState<PaginationState>({ pageIndex: 0, pageSize: 50 });
  const [showCreate, setShowCreate] = useState(false);
  const [mac, setMac] = useState("");
  const [name, setName] = useState("");
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const queryClient = useQueryClient();

  const pod = useQuery({ queryKey: ["pod", podId], queryFn: () => getPod(podId) });
  const sort = sorting.length ? `${sorting[0].desc ? "-" : ""}${sorting[0].id}` : "name";
  const aggregatorId = pod.data?.aggregator?.id;
  const listeners = useQuery({
    queryKey: ["listeners", podId, sort, String(pagination.pageIndex), filter],
    queryFn: () =>
      listListeners({
        aggregator_id: aggregatorId,
        sort,
        limit: pagination.pageSize,
        offset: pagination.pageIndex * pagination.pageSize,
        name: filter || undefined,
      }),
    enabled: aggregatorId !== undefined,
  });
  const create = useMutation({
    mutationFn: (options: { autoSuffix: boolean }) =>
      createListener(
        { mac, name, aggregator_id: aggregatorId ?? "" },
        { autoSuffix: options.autoSuffix },
      ),
    onSuccess: () => {
      setShowCreate(false);
      setMac("");
      setName("");
      setConflict(null);
      void queryClient.invalidateQueries();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "conflict") {
        const detail = error.detail as { field?: string; suggestion?: string } | null;
        if (detail?.field === "name" && detail.suggestion) {
          setConflict({ requestedName: name, suggestion: detail.suggestion });
        }
      }
    },
  });

  const canBulkEdit = useCan("manage_config", pod.data?.deployment_id ?? null);
  const catalog = useQuery({
    queryKey: ["config", "catalog"],
    queryFn: getCatalog,
    enabled: canBulkEdit,
  });

  const rows = listeners.data?.items ?? [];
  const total = listeners.data?.total ?? 0;
  const columns = useMemo(
    () =>
      buildColumns(
        selected,
        rows,
        (toggledMac) =>
          setSelected((current) => {
            const next = new Set(current);
            if (next.has(toggledMac)) {
              next.delete(toggledMac);
            } else {
              next.add(toggledMac);
            }
            return next;
          }),
        () =>
          setSelected((current) =>
            rows.every((item) => current.has(item.mac))
              ? new Set()
              : new Set(rows.map((item) => item.mac)),
          ),
        canBulkEdit,
      ),
    [selected, rows, canBulkEdit],
  );
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

  if (pod.isLoading) {
    return (
      <div data-testid="inventory-loading">
        <div className="skeleton skeleton-row" />
        <div className="skeleton skeleton-row" />
        <p className="skeleton-caption">Loading pod · layout holds final geometry</p>
      </div>
    );
  }
  if (pod.isError || !pod.data) {
    return (
      <EmptyState title="Pod not found" testId="pod-missing">
        It may have been deleted, or it may be outside your assigned scope.
      </EmptyState>
    );
  }

  const row = pod.data;
  return (
    <>
      <PageHeader eyebrow="Pod level" title={row.name}>
        {selected.size > 0 && canBulkEdit && (
          <button
            type="button"
            className="btn-secondary"
            data-testid="bulk-edit-open"
            onClick={() => setBulkOpen(true)}
          >
            Bulk edit ({selected.size})
          </button>
        )}
        <Can permission="manage_devices" deploymentId={row.deployment_id}>
          <button
            type="button"
            onClick={() => setShowCreate((value) => !value)}
            disabled={!row.aggregator}
          >
            New listener
          </button>
        </Can>
      </PageHeader>
      <p className="scope-caption">
        <span className="level-badge">Pod level</span> {row.listener_count} listeners
      </p>
      <TagEditor entity="pods" id={row.id} tags={row.tags} deploymentId={row.deployment_id} />
      <section className="card" data-testid="aggregator-card">
        <h2>Aggregator</h2>
        {row.aggregator ? (
          <dl className="import-summary">
            <dt>aggregator_uuid</dt>
            <dd>{row.aggregator.aggregator_uuid}</dd>
            <dt>balena_uuid</dt>
            <dd>{row.aggregator.balena_uuid ?? "—"}</dd>
            <dt>listeners</dt>
            <dd>{row.aggregator.listener_count}</dd>
          </dl>
        ) : (
          <p className="muted">
            No aggregator attached yet — attach one to start registering listeners.
          </p>
        )}
      </section>
      {row.aggregator && <DeviceTimeline target={{ kind: "aggregator", id: row.aggregator.id }} />}
      {showCreate && row.aggregator && (
        <section className="card">
          <form
            className="form"
            data-testid="create-listener-form"
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate({ autoSuffix: false });
            }}
          >
            <div className="form-field">
              <label htmlFor="new-listener-mac">MAC address</label>
              <input
                id="new-listener-mac"
                value={mac}
                onChange={(event) => setMac(event.target.value)}
                required
              />
              <p className="form-help">
                The immutable device identity. A duplicate MAC is a data-entry error or a cloned
                device; the platform always rejects it.
              </p>
            </div>
            <div className="form-field">
              <label htmlFor="new-listener-name">Name</label>
              <input
                id="new-listener-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
              />
            </div>
            <div className="form-actions">
              <button type="submit" disabled={create.isPending}>
                Create listener
              </button>
              <button type="button" className="btn-tertiary" onClick={() => setShowCreate(false)}>
                Cancel
              </button>
            </div>
            {create.isError && !conflict && (
              <p className="form-error" data-testid="create-listener-error">
                {(create.error as Error).message}
              </p>
            )}
          </form>
        </section>
      )}
      <NameConflictDialog
        open={conflict !== null}
        requestedName={conflict?.requestedName ?? ""}
        suggestedName={conflict?.suggestion ?? ""}
        scopeLabel={row.name}
        onUseSuffix={() => create.mutate({ autoSuffix: true })}
        onEditName={() => setConflict(null)}
      />
      {total === 0 ? (
        <EmptyState title="No listeners yet" testId="listeners-empty">
          Listeners are the ESP32 devices in the field. Register them here, or{" "}
          <Link to="/inventory/import">import a CSV</Link>.
        </EmptyState>
      ) : (
        <EntityTable
          table={table}
          testId="listeners-table"
          caption={`${rows.length} of ${total} shown · sorted by ${sort.replace("-", "")}`}
        />
      )}
      {bulkOpen && (
        <BulkEditModal
          open
          onClose={() => setBulkOpen(false)}
          // The spec 5.2 checkbox path: explicit identities via the E2.5
          // `ids` predicate — normalized server-side, re-scoped per actor.
          selection={{ entity_type: "listener", where: { ids: [...selected].sort() } }}
          selectionLabel={`${selected.size} selected in ${row.name}`}
          catalog={catalog.data?.items ?? []}
          canSaveSelection={canBulkEdit}
        />
      )}
    </>
  );
}
