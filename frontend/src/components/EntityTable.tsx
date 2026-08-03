/**
 * The shared .data-table renderer over a headless TanStack instance (task
 * E1.8; D7 makes the SERVER the source of truth, so tables run
 * manualSorting/manualPagination and the page serializes state to the wire
 * grammar). Column meta.mono marks identifier columns (MACs, uuids,
 * timestamps — "E1's tables are full of these").
 */
import { flexRender, Table as TanstackTable } from "@tanstack/react-table";
import { ReactNode } from "react";

export function EntityTable<T>({
  table,
  caption,
  testId,
  empty,
}: {
  table: TanstackTable<T>;
  caption: string;
  testId?: string;
  empty?: ReactNode;
}) {
  const rows = table.getRowModel().rows;
  const pagination = table.getState().pagination;
  const pageCount = table.getPageCount();
  return (
    <div className="data-table-wrap">
      <table className="data-table" data-testid={testId}>
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => {
                const sortable = header.column.getCanSort();
                const direction = header.column.getIsSorted();
                return (
                  <th
                    key={header.id}
                    aria-sort={
                      direction === "asc"
                        ? "ascending"
                        : direction === "desc"
                          ? "descending"
                          : undefined
                    }
                  >
                    {sortable ? (
                      <button
                        type="button"
                        className="th-sort"
                        onClick={header.column.getToggleSortingHandler()}
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {/* direction marker is CSS-drawn via [aria-sort] — the
                            arrow characters live in no vendored font */}
                        <span className="sort-marker" aria-hidden="true" />
                      </button>
                    ) : (
                      flexRender(header.column.columnDef.header, header.getContext())
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              {row.getVisibleCells().map((cell) => (
                <td
                  key={cell.id}
                  className={
                    (cell.column.columnDef.meta as { mono?: boolean } | undefined)?.mono
                      ? "cell-mono"
                      : undefined
                  }
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && empty}
      <div className="table-foot">
        <span>{caption}</span>
        {pageCount > 1 && (
          <span>
            <button
              type="button"
              className="btn-tertiary"
              disabled={!table.getCanPreviousPage()}
              onClick={() => table.previousPage()}
            >
              Previous
            </button>
            <span className="mono">
              {" "}
              {pagination.pageIndex + 1} / {pageCount}{" "}
            </span>
            <button
              type="button"
              className="btn-tertiary"
              disabled={!table.getCanNextPage()}
              onClick={() => table.nextPage()}
            >
              Next
            </button>
          </span>
        )}
      </div>
    </div>
  );
}
