# E1 Verification Walkthrough

A hands-on test platform for everything Epic E1 shipped — the hierarchy and inventory
system — and for every surface that is *deliberately* still a shell. Work top to bottom,
ticking each box. Every step says what to do and what you should see; if you see anything
else, that is a finding worth writing down.

Timing: 30–45 minutes for the full pass. Sections 1–7 need only the seeded owner account;
section 8 has you create two more accounts.

## 0. Start the platform

- [ ] From the repo root: `.\qa-stack.ps1` (PowerShell). First run builds images (about a
      minute) and prints the owner email and password — **record them now; they are shown
      exactly once.**
- [ ] The script ends with `QA STACK READY` and the site URL `http://localhost:5173`.

Details it automates: `deploy/.env` generation, `docker compose up`, and
`app.seed --demo` ([what the seed does](seed-script.md)). The demo contents are fixed —
exact names, slugs, and MACs live in `docs/INTERFACES.md` under "The demo fixture".

> **Before you ever run the test gate** (`.\gate.ps1` / `make gate`): run
> `.\qa-stack.ps1 down` first. The gate's container tests bind the same four ports and
> will go red against a running QA stack.

Troubleshooting: `.\qa-stack.ps1 status`, or
`docker compose -f deploy/docker-compose.yml -p eoe-qa logs <service>`; the
[getting-started guide](getting-started.md) has a fuller table.

## 1. Sign in and the Organization overview

- [ ] `http://localhost:5173` → **Sign in** → the seeded credentials land you on a page
      titled **Organization overview**.
- [ ] The hero reads **Listeners registered 28** — real data, styled as the one serif
      number on the page. (The label is deliberately *registered*, not *online*: nothing
      is online until Epic 3 wires live devices.)
- [ ] The meta line reads **2 deployments · 6 pods · 28 listeners**.
- [ ] Two deployment cards: **Redwood Coast** (slug `redwood-coast`, 3 pods ·
      16 listeners) and **High Desert** (`high-desert`, 3 pods · 12 listeners), each with
      an "Open inventory" button and the line "Device status arrives with E3 · services
      with E5".
- [ ] The **Needs attention** panel is an empty state naming E3 and E7 — not a fake feed.
- [ ] **Nothing anywhere is showing a device status** — no colored dots, chips, or
      "online" counts. This absence is a designed, test-enforced rule (no invented data,
      ever); status arrives with E3.

## 2. Both themes

- [ ] Click the **Night** toggle in the top bar → the page relights (same layout, dark
      palette). Reload the page → still night (your choice outlives the session and the
      OS preference).
- [ ] Visit Inventory in night mode — tables, tree, and forms all readable; the forest
      backdrop reads as texture behind both themes.
- [ ] Toggle back to day for the rest of the walkthrough (or don't — everything below
      must pass in either theme).

## 3. The hierarchy tree

Open **Inventory**.

- [ ] The left rail shows the tree: **Earth Echoes Demo** → two deployments → three pods
      each → one mono-typed aggregator row per pod (`demo-agg-rc-01`, …).
- [ ] Right-aligned counts: 3 pods per deployment; 8/5/3 and 6/4/2 listeners per pod.
- [ ] Type `alder` in the rail's filter → only the Alder Creek branch remains, with its
      ancestors (Redwood Coast, the org) still visible. Clear it.
- [ ] Click **Pod 01 · Alder Creek** → the pod page opens AND the tree row carries the
      selected treatment (accent tint + left bar).
- [ ] The breadcrumb under the top bar reads org / deployment / pod; the ancestors are
      real links, the final crumb is plain text.
- [ ] Collapse and re-expand a deployment with the caret.

## 4. Tables

- [ ] Every table (deployments on the Inventory index, pods, listeners) has the raised
      uppercase mono header band and 36px rows.
- [ ] Identifiers — MACs, slugs, aggregator uuids, timestamps — render in mono;
      names render in sans.
- [ ] On the listeners table (Alder Creek): click **Name** twice → the order reverses
      (this is a live server round trip, not client sorting).
- [ ] The footer caption reads like `8 of 8 shown · sorted by name`.
- [ ] Type `creek` in the ContextBar's filter box → the table narrows by name.

## 5. Build a hierarchy entirely in the UI (the E1.8 acceptance)

From the Inventory index:

- [ ] **New deployment** → name `QA Ridge` → it appears with slug `qa-ridge` and the
      page navigates into it. (The helper text under the name field explains the slug's
      MQTT role; the freeze-after-first-pod rule is API-enforced and covered by the
      automated suite.)
- [ ] **New pod** → name `QA Pod A`, aggregator uuid left blank → the pod card shows a
      platform-assigned 32-character aggregator uuid.
- [ ] **New pod** → name `QA Pod B`, aggregator uuid `qa-agg-b` → shown verbatim.
- [ ] In QA Pod A: **New listener** → MAC `aa-bb-cc-00-00-01` (lowercase, hyphens), name
      `qa-listener` → the created row shows **AA:BB:CC:00:00:01** (normalized).
- [ ] **New listener** again → different MAC, the SAME name `qa-listener` → the **"Name
      already exists"** dialog appears suggesting `qa-listener-2`. Click **Edit name** →
      dialog closes, nothing was created.
- [ ] Repeat and click **Use suggested name** → `qa-listener-2` appears. (The rename
      happened only because you explicitly accepted it — never silently.)
- [ ] **New listener** with the ALREADY-USED MAC `AA:BB:CC:00:00:01` → an inline error,
      **no dialog** — a duplicate MAC is always rejected, no override exists.
- [ ] Open a listener → **Edit** → set GPS latitude `91` → validation error; set `47.6` /
      `-121.88` → saves and displays in mono.
- [ ] Try **Delete deployment** on QA Ridge now → an error naming exactly what blocks it
      (its pods). Delete the listeners, then each pod, then the deployment → QA Ridge is
      gone from the tree. Nothing in E1 cascades.

## 6. Tags

On any demo deployment:

- [ ] **Edit tags** → add `  coastal  ` (with spaces), `solar`, and `coastal` again →
      **Save tags** → chips read `coastal solar` (trimmed, deduplicated, sorted).
- [ ] Edit again, remove both, add only `ridge` → save → ONLY `ridge` remains (saving
      replaces the whole set; it never merges).
- [ ] A 65-character tag → error, nothing saved.
- [ ] Tag chips also render read-only in the deployment/listener table rows.

## 7. Bulk import

Open **Inventory → Import inventory CSV** (or `/inventory/import`).

- [ ] Paste this CSV (2 valid rows, 1 bad MAC), leave both checkboxes off, **Validate &
      import**:

```csv
mac,name,aggregator_uuid,gps_lat,gps_lon,tags
02:AB:00:00:00:01,qa-import-1,demo-agg-rc-03,47.61,-121.90,qa|walk
02:AB:00:00:00:02,qa-import-2,demo-agg-rc-03,,,
not-a-mac,qa-import-3,demo-agg-rc-03,,,
```

- [ ] The result says **Nothing imported — review the rows**: rows 1–2 read "valid", row
      3 is tinted with a `validation_error` message. Row outcomes are plain colored words
      — deliberately NOT the device-status chips.
- [ ] The **Import 2 rows** button is disabled until you tick the accept checkbox → tick
      it → import → **Imported**, and Tarn Meadow now lists `qa-import-1` (with tags
      `qa walk`) and `qa-import-2`.
- [ ] Re-paste the same CSV with row 1's MAC changed to `02:AB:00:00:00:03` (name
      `qa-import-1` now collides) — with **Auto-suffix colliding names** ticked, the
      committed row lands as `qa-import-1-2`.
- [ ] Switch entity to **Aggregators**, paste a row whose `pod_id` is an occupied demo
      pod (copy a pod id from its page URL) → that row reports a conflict.
- [ ] Paste a CSV with a wrong header (`mac,name,wrong`) → one clear error, no row grid.

## 8. Roles and scoping

On **Users**, create two accounts (any passwords you'll remember for a minute):

- [ ] `qa-viewer@example.com` — role **viewer**, no deployment (org-wide).
- [ ] `qa-operator@example.com` — role **deployment_operator**, deployment id = Redwood
      Coast's UUID (copy it from the address bar on the Redwood Coast page).

In a private/incognito window per account:

- [ ] **Both roles**: the top-bar nav lists every destination — access is enforced on
      page contents, never by hiding nav (a deliberate design decision).
- [ ] **Viewer**: Inventory renders every table and tag chip, but there are NO "New …",
      "Edit", "Delete", or "Edit tags" controls anywhere; `/inventory/import` shows the
      access panel instead of the form; `/users` shows the denied panel.
- [ ] **Operator (Redwood Coast)**: the tree and every list show Redwood Coast ONLY —
      High Desert does not exist anywhere in their UI.
- [ ] Paste a High Desert pod URL (grab one as the owner) into the operator's window →
      a "Pod not found" panel. Deliberate: out-of-scope and nonexistent look identical,
      so device identifiers can't be probed.
- [ ] The operator CAN create a pod inside Redwood Coast (writes work in scope).
- [ ] Back as the owner: delete both `qa-*` accounts' access when done (deactivate on
      the Users page).

## 9. Shells-of-features audit

Every surface E1 does NOT fill must say exactly which epic fills it — no fakes, no
placeholders pretending to be data:

- [ ] **/map** — the reserved map region plus the six-state status legend (the one place
      the status vocabulary legitimately shows, as a legend); the copy names **E6**.
- [ ] **/configuration** — ~~empty state naming E2~~ *(amended 2026-08-04, E2.7: the
      live inheritance editor now lives here — verify it via
      [the E2 walkthrough](e2-verification.md); its Publish affordances name **E3**)*.
- [ ] **/provisioning** — empty state naming **E4** and **E5**.
- [ ] **Overview → Needs attention** — names **E3/E7**.
- [ ] **Deployment cards** — "Device status arrives with E3 · services with E5".
- [ ] **Listener detail** — footer "Live status arrives with E3 · telemetry with E5"
      *(amended 2026-08-04, E2.7: the "effective config with E2" promise is delivered —
      the page now carries an **Effective config** card with an Edit deep-link into
      /configuration)*; GPS fields are plain inputs (the guided flow is E4.11).
- [ ] **Empty states with actions** — an empty pod offers listener creation and the
      import link; a fresh org would offer "New deployment" + "Import inventory CSV".

## 10. System page and wrap-up

- [ ] **/system** shows live health with the build identifier (served by the real API).
- [ ] Optional but recommended: `cd backend` then `uv run python -m app.verify` with
      `DATABASE_URL` pointing at `localhost:5432` (the values live in `deploy/.env`) —
      expect every `[PASS]`, including the 11 hierarchy steps
      ([what it checks](verify-deployment.md)).
- [ ] Done: `.\qa-stack.ps1 down` (keeps your QA data) or `reset` (wipes it).
- [ ] **Reminder:** the stack must be down before `.\gate.ps1`.
