# Bulk import: listeners and aggregators from CSV or JSON

Register many devices in one request instead of one form at a time. The same importer
backs the Inventory screen's "Import" flow and these API calls. The column format below
is fixed by `docs/INTERFACES.md` ("Bulk import") — if this page and that section ever
disagree, INTERFACES wins.

## Listener CSV

Header must be exactly this, in this order:

```csv
mac,name,aggregator_uuid,gps_lat,gps_lon,tags
AA:BB:CC:DD:EE:01,alder-creek-01,agg-alder-01,47.6412,-121.8871,coastal|solar
AA:BB:CC:DD:EE:02,alder-creek-02,agg-alder-01,,,
```

- `mac` — any common form (`AA:BB:...`, `aa-bb-...`, bare hex); stored normalized.
- `aggregator_uuid` — the parent aggregator's platform join key (not its row id).
- `gps_lat`/`gps_lon` — optional; leave blank until surveyed.
- `tags` — optional, pipe-separated (`coastal|solar`).

## Aggregator CSV

```csv
pod_id,aggregator_uuid,balena_uuid,name,tags
6f1d2c3b-...,agg-ridge-02,,ridge-gateway,ridge
7a2e3d4c-...,,,,
```

Leave `aggregator_uuid` blank and the platform assigns one (the recommended path).

## Sending it

```bash
curl -X POST "$API/api/v1/listeners/import?partial=false&auto_suffix=false" \
  -H "Content-Type: text/csv" \
  -H "X-CSRF-Token: $CSRF" \
  --cookie "$COOKIES" \
  --data-binary @listeners.csv
```

JSON works too: `POST /listeners/import` with `{"rows": [{...}, ...]}` and
`Content-Type: application/json`. Limits: 1000 rows, 1 MiB per request.

## How results come back

Always a `200` with a per-row report — check `committed`:

- **All-or-nothing (default):** if any row fails, *nothing* is imported and
  `committed` is `false`. The `rows` list tells you exactly which lines failed and why
  (row numbers count data rows, not the header). Fix the file and resend — or resend
  unchanged with `?partial=true` to accept the valid rows and skip the failures.
- **`?partial=true`:** valid rows import; failed rows are reported and skipped.
- **`?auto_suffix=true`** (listeners only): a name that already exists in the
  deployment imports as `name-2`, `name-3`, … Without it, name collisions fail the row.
  A duplicate MAC always fails its row — a cloned MAC is a data-entry error the
  platform refuses to paper over.

Every import writes one audit entry recording counts and the created ids.
